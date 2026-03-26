#!/usr/bin/env python3
"""Thin MCP server exposing only knowledge collaboration tools.

Shares the same SQLite database as the main sqlite-kb server.
Exists because Claude Code 2.x has a tool-count limit per MCP server
(~9 tools visible out of 50), so collab tools are split into a separate server.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastmcp import FastMCP

from db_utils import (
    get_conn as _get_conn,
    get_entity_id as _get_entity_id,
    fts_query as _fts_query,
    fts_sync_entity as _fts_sync,
    now_iso as _now,
    TASK_PRIORITIES as _TASK_PRIORITIES,
    TRUST_LEVELS as _TRUST_LEVELS,
    PUBLISH_STANDBY_MINUTES as _PUBLISH_STANDBY_MINUTES,
    IQ_WEIGHTS as _IQ_WEIGHTS,
    TIER_WEIGHTS as _TIER_WEIGHTS,
    VERIFICATION_OUTCOMES as _VERIFICATION_OUTCOMES,
    VERIFICATION_WEIGHTS as _VERIFICATION_WEIGHTS,
    RATING_BURST_THRESHOLD as _RATING_BURST_THRESHOLD,
    RATING_BURST_WINDOW_HOURS as _RATING_BURST_WINDOW_HOURS,
)
from schema import (
    init_db,
    error as _error,
)

# ── Logging (file-only, NEVER stdout — breaks MCP stdio) ────────────────
from db_utils import setup_logger

logger = setup_logger("sqlite-collab", "collab_server.log")

# ── FastMCP app ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-collab",
    instructions=(
        "Knowledge collaboration tools: P2P sharing, public knowledge, ratings, verification. "
        "Shares DB with sqlite-kb."
    ),
)

# ── Init DB ──────────────────────────────────────────────────────────────
init_db()


# ═══════════════════════════════════════════════════════════════════════════
# Private helpers (used only by collab tools)
# ═══════════════════════════════════════════════════════════════════════════


def _content_hash(entity_name: str, observations: list[str]) -> str:
    """Deterministic SHA256 bound to exact content version (order-independent)."""
    raw = json.dumps({"name": entity_name, "obs": sorted(observations)}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _entity_content_hash(conn, entity_name: str) -> tuple[str, list[str]] | None:
    """Fetch observations + compute content hash. Returns (hash, obs_list) or None."""
    obs_rows = conn.execute(
        "SELECT o.content FROM observations o "
        "JOIN entities e ON o.entity_id = e.id "
        "WHERE e.name = ? ORDER BY o.id",
        (entity_name,),
    ).fetchall()
    if not obs_rows:
        return None
    obs = [r["content"] for r in obs_rows]
    return _content_hash(entity_name, obs), obs


def _get_publisher_id(conn, entity_name: str) -> str:
    """Extract publisher identity for an entity."""
    row = conn.execute(
        "SELECT origin, shared_by FROM entities WHERE name = ?", (entity_name,)
    ).fetchone()
    if not row:
        return ""
    origin = row["origin"] or "local"
    if origin.startswith("shared:"):
        return origin.split(":", 1)[1]
    if row["shared_by"]:
        return row["shared_by"]
    return os.environ.get("GITHUB_USER", socket.gethostname())


def _compute_truth_score(entity_name: str, conn) -> dict[str, Any]:
    """Compute composite TruthScore for a public entity.

    Three tiers: IQ (content quality), Verification, Cross-validation.
    Returns dict with truth_score, confidence, rating_count, content_hash, dimensions.
    """
    # Get current content hash
    result = _entity_content_hash(conn, entity_name)
    observations = result[1] if result else []
    c_hash = result[0] if result else _content_hash(entity_name, [])

    # Get ratings for current content version
    ratings = conn.execute(
        "SELECT specificity, falsifiability, internal_consistency, novelty, "
        "verification_outcome, usefulness FROM knowledge_ratings "
        "WHERE entity_name = ? AND content_hash = ?",
        (entity_name, c_hash),
    ).fetchall()

    if not ratings:
        return {
            "truth_score": 0.0,
            "confidence": 0.0,
            "rating_count": 0,
            "content_hash": c_hash,
            "dimensions": {},
        }

    rater_count = len(ratings)

    # Tier 1: IQ — average of dimensional scores
    avg_spec = sum(r["specificity"] for r in ratings) / rater_count
    avg_fals = sum(r["falsifiability"] for r in ratings) / rater_count
    avg_cons = sum(r["internal_consistency"] for r in ratings) / rater_count
    avg_nov = sum(r["novelty"] for r in ratings) / rater_count

    iq = (
        _IQ_WEIGHTS["specificity"] * avg_spec
        + _IQ_WEIGHTS["falsifiability"] * avg_fals
        + _IQ_WEIGHTS["internal_consistency"] * avg_cons
        + _IQ_WEIGHTS["novelty"] * avg_nov
    )

    # Tier 2: Verification — avg(usefulness * weight) for verified ratings
    verified = [r for r in ratings if r["verification_outcome"] is not None]
    if verified:
        v_scores = []
        for r in verified:
            w = _VERIFICATION_WEIGHTS.get(r["verification_outcome"], 0.5)
            u = r["usefulness"] if r["usefulness"] is not None else 0.5
            v_scores.append(u * w)
        v = sum(v_scores) / len(v_scores)
    else:
        v = 0.5  # neutral if no verifications

    # Tier 3: Cross-validation — log-diminishing returns on confirmed count
    confirmed_count = sum(
        1 for r in ratings if r["verification_outcome"] == "confirmed"
    )
    cv = min(1.0, math.log2(confirmed_count + 1) / 4.0)

    # Confidence scales with rater count (log-diminishing)
    confidence = min(1.0, 0.5 + 0.15 * math.log2(rater_count + 1))

    # Adaptive weights: shift toward IQ if no verifications
    if not verified:
        iq_w, v_w, cv_w = 0.55, 0.20, 0.25
    else:
        iq_w = _TIER_WEIGHTS["iq"]
        v_w = _TIER_WEIGHTS["verification"]
        cv_w = _TIER_WEIGHTS["cross_validation"]

    truth_score = (iq_w * iq + v_w * v + cv_w * cv) * confidence

    return {
        "truth_score": round(truth_score, 4),
        "confidence": round(confidence, 4),
        "rating_count": rater_count,
        "content_hash": c_hash,
        "dimensions": {
            "specificity": round(avg_spec, 4),
            "falsifiability": round(avg_fals, 4),
            "internal_consistency": round(avg_cons, 4),
            "novelty": round(avg_nov, 4),
            "iq_composite": round(iq, 4),
            "verification": round(v, 4),
            "cross_validation": round(cv, 4),
        },
    }


def _check_rating_anomalies(conn, entity_name: str) -> None:
    """Detect rating burst anomalies (too many ratings in short window)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=_RATING_BURST_WINDOW_HOURS)
    ).isoformat()
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM knowledge_ratings "
        "WHERE entity_name = ? AND rated_at >= ?",
        (entity_name, cutoff),
    ).fetchone()["cnt"]

    if count > _RATING_BURST_THRESHOLD:
        conn.execute(
            "INSERT INTO rating_anomalies (entity_name, anomaly_type, details, detected_at) "
            "VALUES (?, ?, ?, ?)",
            (
                entity_name,
                "rating_burst",
                f"{count} ratings in {_RATING_BURST_WINDOW_HOURS}h (threshold: {_RATING_BURST_THRESHOLD})",
                _now(),
            ),
        )
        logger.warning(
            "Rating anomaly detected: %s has %d ratings in %dh",
            entity_name,
            count,
            _RATING_BURST_WINDOW_HOURS,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 1: manage_collaborators
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def manage_collaborators(
    action: str,
    github_user: str | None = None,
    display_name: str | None = None,
    trust_level: str | None = None,
    notes: str | None = None,
) -> str:
    """Manage the collaborator address book for P2P knowledge sharing.

    Each collaborator is a GitHub user whose memory-bridge repo you can
    push knowledge to and pull knowledge from.

    Args:
        action: add | remove | list | update.
        github_user: GitHub username (required for add/remove/update).
        display_name: Human-friendly name.
        trust_level: read_only (you push, they can't push back) | read_write (bidirectional).
        notes: Free-text notes about this collaborator.
    """
    if action not in ("add", "remove", "list", "update"):
        return _error("action must be: add, remove, list, update")

    with _get_conn() as conn:
        if action == "list":
            rows = conn.execute(
                "SELECT * FROM collaborators ORDER BY added_at"
            ).fetchall()
            items = [dict(r) for r in rows]
            return json.dumps({"collaborators": items, "count": len(items)})

        if not github_user:
            return _error("github_user required for add/remove/update")

        if action == "add":
            tl = trust_level or "read_write"
            if tl not in _TRUST_LEVELS:
                return _error(f"trust_level must be one of: {', '.join(_TRUST_LEVELS)}")
            now = _now()
            conn.execute(
                "INSERT INTO collaborators "
                "(github_user, display_name, trust_level, added_at, notes) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(github_user) DO UPDATE SET "
                "display_name=excluded.display_name, trust_level=excluded.trust_level, "
                "notes=excluded.notes",
                (github_user, display_name, tl, now, notes),
            )
            logger.info("manage_collaborators: added %s (trust=%s)", github_user, tl)
            return json.dumps(
                {"added": github_user, "trust_level": tl, "display_name": display_name}
            )

        if action == "remove":
            cur = conn.execute(
                "DELETE FROM collaborators WHERE github_user = ?", (github_user,)
            )
            # Also clean up sharing rules targeting this user
            conn.execute(
                "DELETE FROM sharing_rules WHERE target_user = ?", (github_user,)
            )
            if cur.rowcount == 0:
                return _error(f"Collaborator '{github_user}' not found")
            logger.info("manage_collaborators: removed %s", github_user)
            return json.dumps({"removed": github_user})

        # action == "update"
        existing = conn.execute(
            "SELECT * FROM collaborators WHERE github_user = ?", (github_user,)
        ).fetchone()
        if not existing:
            return _error(f"Collaborator '{github_user}' not found")

        updates = {}
        if display_name is not None:
            updates["display_name"] = display_name
        if trust_level is not None:
            if trust_level not in _TRUST_LEVELS:
                return _error(f"trust_level must be one of: {', '.join(_TRUST_LEVELS)}")
            updates["trust_level"] = trust_level
        if notes is not None:
            updates["notes"] = notes
        if not updates:
            return _error("Nothing to update")

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE collaborators SET {set_clause} WHERE github_user = ?",
            list(updates.values()) + [github_user],
        )
        logger.info("manage_collaborators: updated %s (%s)", github_user, list(updates))
        return json.dumps({"updated": github_user, "fields": list(updates.keys())})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 2: share_knowledge
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def share_knowledge(
    entity_names: list[str],
    target_users: list[str] | None = None,
    include_relations: bool = True,
    priority: str = "medium",
) -> str:
    """Queue entities for sharing with collaborators on next bridge_push.

    Creates sharing rules — does NOT push immediately.
    P2P priority signals how urgently the recipient should adopt this knowledge.

    Args:
        entity_names: Entity names to share (or ['*'] for all shared-tagged).
        target_users: GitHub usernames (or ['*'] for all collaborators). Defaults to all.
        include_relations: Also share inter-relations between the named entities.
        priority: critical | high | medium | low — urgency signal for recipients.
    """
    if priority not in _TASK_PRIORITIES:
        return _error(f"priority must be one of: {', '.join(_TASK_PRIORITIES)}")

    with _get_conn() as conn:
        # Resolve target users
        if not target_users or target_users == ["*"]:
            collab_rows = conn.execute(
                "SELECT github_user FROM collaborators"
            ).fetchall()
            targets = [r["github_user"] for r in collab_rows]
        else:
            targets = target_users

        if not targets:
            return _error(
                "No collaborators found. Use manage_collaborators(action='add') first."
            )

        # Validate entities exist (unless wildcard)
        if entity_names != ["*"]:
            for name in entity_names:
                row = conn.execute(
                    "SELECT 1 FROM entities WHERE name = ?", (name,)
                ).fetchone()
                if not row:
                    return _error(f"Entity '{name}' not found")

        share_types = ["entity"]
        if include_relations:
            share_types.append("relation")

        created = 0
        now = _now()
        for ename in entity_names:
            for tuser in targets:
                for stype in share_types:
                    cur = conn.execute(
                        "INSERT OR REPLACE INTO sharing_rules "
                        "(entity_name, target_user, share_type, priority, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (ename, tuser, stype, priority, now),
                    )
                    created += cur.rowcount

        logger.info(
            "share_knowledge: %d rules created for %d entities → %d users (priority=%s)",
            created,
            len(entity_names),
            len(targets),
            priority,
        )
        return json.dumps(
            {
                "rules_created": created,
                "entities": entity_names,
                "targets": targets,
                "include_relations": include_relations,
                "priority": priority,
                "message": f"Queued for next bridge_push. {len(targets)} recipient(s).",
            }
        )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 3: review_shared_knowledge
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def review_shared_knowledge(
    action: str = "list",
    item_ids: list[int] | None = None,
) -> str:
    """Review incoming shared knowledge from collaborators.

    All cross-account entities enter staging first — never auto-imported.
    P2P priority (critical/high/medium/low) indicates sender's urgency signal.

    Args:
        action: list | approve | reject | diff.
        item_ids: IDs from pending_shared_entities to act on. If None, applies to ALL.
    """
    if action not in ("list", "approve", "reject", "diff"):
        return _error("action must be: list, approve, reject, diff")

    with _get_conn() as conn:
        if action == "list":
            ent_rows = conn.execute(
                "SELECT id, name, entity_type, project, priority, shared_by, received_at "
                "FROM pending_shared_entities ORDER BY "
                "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 END, received_at DESC"
            ).fetchall()
            rel_rows = conn.execute(
                "SELECT id, from_entity, to_entity, relation_type, shared_by, received_at "
                "FROM pending_shared_relations ORDER BY received_at DESC"
            ).fetchall()
            return json.dumps(
                {
                    "pending_entities": [dict(r) for r in ent_rows],
                    "pending_relations": [dict(r) for r in rel_rows],
                    "entity_count": len(ent_rows),
                    "relation_count": len(rel_rows),
                }
            )

        if action == "diff":
            if not item_ids:
                return _error("item_ids required for diff")
            diffs = []
            for iid in item_ids:
                pending = conn.execute(
                    "SELECT * FROM pending_shared_entities WHERE id = ?", (iid,)
                ).fetchone()
                if not pending:
                    diffs.append({"id": iid, "error": "not found"})
                    continue
                p = dict(pending)
                raw_obs = json.loads(p["observations"])
                if not isinstance(raw_obs, list):
                    raw_obs = []
                pending_obs = raw_obs[:1000]
                local_id = _get_entity_id(conn, p["name"])
                if not local_id:
                    diffs.append(
                        {
                            "id": iid,
                            "name": p["name"],
                            "status": "new_entity",
                            "remote_type": p["entity_type"],
                            "remote_observations": len(pending_obs),
                            "priority": p["priority"],
                        }
                    )
                else:
                    local_obs = conn.execute(
                        "SELECT content FROM observations WHERE entity_id = ?",
                        (local_id,),
                    ).fetchall()
                    local_contents = {r["content"] for r in local_obs}
                    remote_contents = {
                        o["content"] if isinstance(o, dict) else o for o in pending_obs
                    }
                    local_etype = conn.execute(
                        "SELECT entity_type FROM entities WHERE id = ?", (local_id,)
                    ).fetchone()["entity_type"]
                    diffs.append(
                        {
                            "id": iid,
                            "name": p["name"],
                            "status": "type_conflict"
                            if local_etype != p["entity_type"]
                            else "merge",
                            "local_type": local_etype,
                            "remote_type": p["entity_type"],
                            "new_observations": list(remote_contents - local_contents),
                            "already_have": len(local_contents & remote_contents),
                            "priority": p["priority"],
                        }
                    )
            return json.dumps({"diffs": diffs})

        # Build WHERE for specific IDs or all
        if item_ids:
            ph = ",".join("?" * len(item_ids))
            ent_where = f"id IN ({ph})"
            ent_params: list = list(item_ids)
        else:
            ent_where = "1=1"
            ent_params = []

        if action == "approve":
            rows = conn.execute(
                f"SELECT * FROM pending_shared_entities WHERE {ent_where}", ent_params
            ).fetchall()
            imported_entities = 0
            imported_obs = 0
            now = _now()
            approved_names: set[str] = set()
            for row in rows:
                p = dict(row)
                pending_obs = json.loads(p["observations"])
                origin = f"shared:{p['shared_by']}"

                # Upsert entity (additive — never overwrites local)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO entities "
                    "(name, entity_type, project, shared_by, origin, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        p["name"],
                        p["entity_type"],
                        p.get("project"),
                        p["shared_by"],
                        origin,
                        now,
                        now,
                    ),
                )
                imported_entities += cur.rowcount
                approved_names.add(p["name"])

                eid = _get_entity_id(conn, p["name"])
                if eid:
                    for obs in pending_obs:
                        content = (
                            obs.get("content") or obs.get("text")
                            if isinstance(obs, dict)
                            else obs
                        )
                        if not content:
                            continue
                        created = (
                            obs.get("createdAt", now) if isinstance(obs, dict) else now
                        )
                        cur2 = conn.execute(
                            "INSERT OR IGNORE INTO observations "
                            "(entity_id, content, created_at) VALUES (?, ?, ?)",
                            (eid, content, created),
                        )
                        imported_obs += cur2.rowcount
                    _fts_sync(conn, eid)

                conn.execute(
                    "DELETE FROM pending_shared_entities WHERE id = ?", (p["id"],)
                )

            # Also approve matching pending relations (only for approved entities)
            rel_rows = conn.execute("SELECT * FROM pending_shared_relations").fetchall()
            imported_rels = 0
            for rel in rel_rows:
                r = dict(rel)
                if (
                    r["from_entity"] not in approved_names
                    or r["to_entity"] not in approved_names
                ):
                    continue
                from_id = _get_entity_id(conn, r["from_entity"])
                to_id = _get_entity_id(conn, r["to_entity"])
                if from_id and to_id:
                    cur3 = conn.execute(
                        "INSERT OR IGNORE INTO relations "
                        "(from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
                        (from_id, to_id, r["relation_type"], now),
                    )
                    imported_rels += cur3.rowcount
                    conn.execute(
                        "DELETE FROM pending_shared_relations WHERE id = ?", (r["id"],)
                    )

            logger.info(
                "review_shared_knowledge: approved %d entities, %d obs, %d relations",
                imported_entities,
                imported_obs,
                imported_rels,
            )
            return json.dumps(
                {
                    "approved_entities": imported_entities,
                    "new_observations": imported_obs,
                    "approved_relations": imported_rels,
                }
            )

        # action == "reject"
        cur_e = conn.execute(
            f"DELETE FROM pending_shared_entities WHERE {ent_where}", ent_params
        )
        # If no specific IDs, also clear all pending relations
        if not item_ids:
            cur_r = conn.execute("DELETE FROM pending_shared_relations")
            rejected_rels = cur_r.rowcount
        else:
            rejected_rels = 0
        rejected = cur_e.rowcount
        logger.info(
            "review_shared_knowledge: rejected %d entities, %d relations",
            rejected,
            rejected_rels,
        )
        return json.dumps(
            {"rejected_entities": rejected, "rejected_relations": rejected_rels}
        )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 4: request_publish
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def request_publish(
    entity_names: list[str] | None = None,
    task_ids: list[str] | None = None,
    safety_confirmed: bool = False,
) -> str:
    """Request to publish entities/tasks as public knowledge.

    ⚠️ WARNING 1: Publishing makes content visible to ALL instances.
    Default action is to NOT publish. You must explicitly set safety_confirmed=True.

    ⚠️ WARNING 2: Before confirming, verify the content will not harm,
    endanger, or compromise the safety of any person.

    After confirmation, content enters a standby period (default 15 min)
    before becoming truly public on next bridge_push.
    """
    if not entity_names and not task_ids:
        return _error("Provide entity_names and/or task_ids")

    if not safety_confirmed:
        return json.dumps(
            {
                "status": "confirmation_required",
                "recommendation": (
                    "P2P Knowledge Sharing targets SPECIFIC technical "
                    "information useful to other machines/agents in the "
                    "network — not generic knowledge, but hard-won lessons. "
                    "Ideal candidates: verified gotchas, non-obvious patterns, "
                    "environment-specific bugs with confirmed workarounds. "
                    "Each item should be: specific (not generic), falsifiable "
                    "(can be tested), novel (hard to discover independently), "
                    "and universal (applies beyond one project)."
                ),
                "warning_1": (
                    "⚠️ You are about to make content PUBLIC and visible to "
                    "ALL Claude instances. Default: DO NOT publish."
                ),
                "warning_2": (
                    "⚠️ Are you sure the content will NOT harm, endanger, "
                    "or compromise the safety of any person?"
                ),
                "action": "Call request_publish again with safety_confirmed=True to proceed.",
                "standby_minutes": _PUBLISH_STANDBY_MINUTES,
            }
        )

    now = _now()
    updated_entities = 0
    updated_tasks = 0
    not_found: list[str] = []

    with _get_conn() as conn:
        for name in entity_names or []:
            cur = conn.execute(
                "UPDATE entities SET visibility='pending_public', "
                "publish_requested_at=?, updated_at=? "
                "WHERE name=? AND visibility='private'",
                (now, now, name),
            )
            if cur.rowcount:
                updated_entities += cur.rowcount
            else:
                # Check if it exists at all
                row = conn.execute(
                    "SELECT visibility FROM entities WHERE name=?", (name,)
                ).fetchone()
                if not row:
                    not_found.append(f"entity:{name}")
                # else already pending/public — skip silently

        for tid in task_ids or []:
            cur = conn.execute(
                "UPDATE tasks SET visibility='pending_public', "
                "publish_requested_at=?, updated_at=? "
                "WHERE id=? AND visibility='private'",
                (now, now, tid),
            )
            if cur.rowcount:
                updated_tasks += cur.rowcount
            else:
                row = conn.execute(
                    "SELECT visibility FROM tasks WHERE id=?", (tid,)
                ).fetchone()
                if not row:
                    not_found.append(f"task:{tid}")

    logger.info(
        "request_publish: %d entities, %d tasks set to pending_public",
        updated_entities,
        updated_tasks,
    )
    result: dict[str, Any] = {
        "status": "pending_public",
        "entities_updated": updated_entities,
        "tasks_updated": updated_tasks,
        "standby_minutes": _PUBLISH_STANDBY_MINUTES,
        "message": (
            f"Content will become public after {_PUBLISH_STANDBY_MINUTES} min "
            "standby on next bridge_push."
        ),
    }
    if not_found:
        result["not_found"] = not_found
    return json.dumps(result)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 5: cancel_publish
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def cancel_publish(
    entity_names: list[str] | None = None,
    task_ids: list[str] | None = None,
) -> str:
    """Cancel a pending publish request. Reverts pending_public → private.

    Only works during the standby period (before content becomes truly public).
    """
    if not entity_names and not task_ids:
        return _error("Provide entity_names and/or task_ids")

    now = _now()
    reverted_entities = 0
    reverted_tasks = 0

    with _get_conn() as conn:
        for name in entity_names or []:
            cur = conn.execute(
                "UPDATE entities SET visibility='private', "
                "publish_requested_at=NULL, updated_at=? "
                "WHERE name=? AND visibility='pending_public'",
                (now, name),
            )
            reverted_entities += cur.rowcount

        for tid in task_ids or []:
            cur = conn.execute(
                "UPDATE tasks SET visibility='private', "
                "publish_requested_at=NULL, updated_at=? "
                "WHERE id=? AND visibility='pending_public'",
                (now, tid),
            )
            reverted_tasks += cur.rowcount

    logger.info(
        "cancel_publish: reverted %d entities, %d tasks to private",
        reverted_entities,
        reverted_tasks,
    )
    return json.dumps(
        {
            "reverted_entities": reverted_entities,
            "reverted_tasks": reverted_tasks,
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 6: search_public_knowledge
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def search_public_knowledge(
    query: str,
    entity_type: str | None = None,
    sort_by: str = "relevance",
    min_truth_score: float | None = None,
    limit: int = 50,
) -> str:
    """Search published public knowledge using FTS5 BM25-ranked search.

    Only returns entities with visibility='public'.

    Args:
        sort_by: "relevance" (BM25), "truth_score", or "rating_count"
        min_truth_score: Filter out entities below this TruthScore threshold
    """
    fts_q = _fts_query(query)
    with _get_conn() as conn:
        if entity_type:
            rows = conn.execute(
                "SELECT memory_fts.rowid, memory_fts.name, memory_fts.entity_type, "
                "memory_fts.observations_text, memory_fts.rank "
                "FROM memory_fts "
                "JOIN entities ON entities.id = memory_fts.rowid "
                "WHERE memory_fts MATCH ? AND entities.visibility = 'public' "
                "AND entities.entity_type = ? "
                "ORDER BY memory_fts.rank LIMIT ?",
                (fts_q, entity_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT memory_fts.rowid, memory_fts.name, memory_fts.entity_type, "
                "memory_fts.observations_text, memory_fts.rank "
                "FROM memory_fts "
                "JOIN entities ON entities.id = memory_fts.rowid "
                "WHERE memory_fts MATCH ? AND entities.visibility = 'public' "
                "ORDER BY memory_fts.rank LIMIT ?",
                (fts_q, limit),
            ).fetchall()

        # Batch-fetch observations (avoids N+1 per-entity queries)
        if rows:
            eids = [r["rowid"] for r in rows]
            ph = ",".join("?" * len(eids))
            obs_rows = conn.execute(
                f"SELECT entity_id, content FROM observations "
                f"WHERE entity_id IN ({ph}) ORDER BY entity_id, id",
                eids,
            ).fetchall()
            obs_by_eid: dict[int, list[str]] = {}
            for o in obs_rows:
                obs_by_eid.setdefault(o["entity_id"], []).append(o["content"])
        else:
            obs_by_eid = {}

        results = []
        for r in rows:
            score_info = _compute_truth_score(r["name"], conn)
            if (
                min_truth_score is not None
                and score_info["truth_score"] < min_truth_score
            ):
                continue
            results.append(
                {
                    "name": r["name"],
                    "entityType": r["entity_type"],
                    "observations": obs_by_eid.get(r["rowid"], []),
                    "truthScore": score_info["truth_score"],
                    "ratingCount": score_info["rating_count"],
                    "confidence": score_info["confidence"],
                }
            )

        # Sort results
        if sort_by == "truth_score":
            results.sort(key=lambda x: x["truthScore"], reverse=True)
        elif sort_by == "rating_count":
            results.sort(key=lambda x: x["ratingCount"], reverse=True)

    logger.info("search_public_knowledge: query=%r matched=%d", query, len(results))
    return json.dumps({"entities": results, "query": query, "count": len(results)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 7: rate_public_knowledge
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def rate_public_knowledge(
    entity_name: str,
    specificity: float,
    falsifiability: float,
    internal_consistency: float,
    novelty: float,
    verification_outcome: str | None = None,
    usefulness: float | None = None,
    verification_context: str | None = None,
) -> str:
    """Rate a public knowledge entity's quality (Claude-only structured analysis).

    Anti-gaming: rater_id set server-side, content_hash computed from DB,
    self-rating blocked, UNIQUE constraint prevents re-rating same version.

    Args:
        entity_name: Name of the public entity to rate
        specificity: How specific/precise the knowledge is (0.0-1.0)
        falsifiability: Can claims be tested/verified? (0.0-1.0)
        internal_consistency: Are observations consistent? (0.0-1.0)
        novelty: Does it add new information? (0.0-1.0)
        verification_outcome: "confirmed", "contradicted", or "inconclusive"
        usefulness: How useful was the knowledge in practice? (0.0-1.0)
        verification_context: Description of how verification was done
    """
    # Validate scores in [0.0, 1.0]
    for name, val in [
        ("specificity", specificity),
        ("falsifiability", falsifiability),
        ("internal_consistency", internal_consistency),
        ("novelty", novelty),
    ]:
        if not (0.0 <= val <= 1.0):
            return _error(f"{name} must be between 0.0 and 1.0, got {val}")

    if verification_outcome is not None:
        if verification_outcome not in _VERIFICATION_OUTCOMES:
            return _error(
                f"verification_outcome must be one of {_VERIFICATION_OUTCOMES}"
            )
        if usefulness is None:
            return _error(
                "usefulness is required when verification_outcome is provided"
            )

    if usefulness is not None and not (0.0 <= usefulness <= 1.0):
        return _error(f"usefulness must be between 0.0 and 1.0, got {usefulness}")

    # rater_id: server-side identity (never user input)
    rater_id = os.environ.get("GITHUB_USER", socket.gethostname())

    with _get_conn() as conn:
        # Entity must exist and be public
        entity = conn.execute(
            "SELECT name, visibility FROM entities WHERE name = ?", (entity_name,)
        ).fetchone()
        if not entity:
            return _error(f"Entity '{entity_name}' not found")
        if entity["visibility"] != "public":
            return _error(
                f"Entity '{entity_name}' is not public (visibility={entity['visibility']})"
            )

        # Anti-gaming: no self-rating
        publisher_id = _get_publisher_id(conn, entity_name)
        if rater_id == publisher_id:
            return _error("Cannot rate your own published knowledge")

        # Compute content hash from current DB content
        result = _entity_content_hash(conn, entity_name)
        c_hash = result[0] if result else _content_hash(entity_name, [])

        # Insert rating (UNIQUE constraint prevents re-rating same version)
        try:
            conn.execute(
                "INSERT INTO knowledge_ratings "
                "(entity_name, rater_id, content_hash, specificity, falsifiability, "
                "internal_consistency, novelty, verification_outcome, usefulness, "
                "verification_context, rated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_name,
                    rater_id,
                    c_hash,
                    specificity,
                    falsifiability,
                    internal_consistency,
                    novelty,
                    verification_outcome,
                    usefulness,
                    verification_context,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError:
            return json.dumps(
                {
                    "error": "Already rated this content version",
                    "hint": "Content must change before you can rate again",
                }
            )

        # Anomaly detection
        _check_rating_anomalies(conn, entity_name)

        # Compute updated TruthScore
        score_info = _compute_truth_score(entity_name, conn)

    logger.info(
        "rate_public_knowledge: %s rated by %s (score=%.4f)",
        entity_name,
        rater_id,
        score_info["truth_score"],
    )
    return json.dumps(
        {
            "status": "rated",
            "entity_name": entity_name,
            "rater_id": rater_id,
            "content_hash": c_hash,
            **score_info,
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 8: get_knowledge_ratings
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_knowledge_ratings(
    entity_name: str,
    include_individual: bool = False,
) -> str:
    """Get computed TruthScore and dimensional breakdown for a public entity.

    Args:
        entity_name: Name of the entity to get ratings for
        include_individual: Include individual rating details
    """
    with _get_conn() as conn:
        entity = conn.execute(
            "SELECT name, visibility FROM entities WHERE name = ?", (entity_name,)
        ).fetchone()
        if not entity:
            return _error(f"Entity '{entity_name}' not found")

        score_info = _compute_truth_score(entity_name, conn)

        # Anomaly status
        anomaly_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM rating_anomalies "
            "WHERE entity_name = ? AND resolved = 0",
            (entity_name,),
        ).fetchone()["cnt"]
        score_info["unresolved_anomalies"] = anomaly_count

        if include_individual:
            ratings = conn.execute(
                "SELECT rater_id, specificity, falsifiability, internal_consistency, "
                "novelty, verification_outcome, usefulness, verification_context, rated_at "
                "FROM knowledge_ratings WHERE entity_name = ? AND content_hash = ? "
                "ORDER BY rated_at",
                (entity_name, score_info["content_hash"]),
            ).fetchall()
            score_info["individual_ratings"] = [dict(r) for r in ratings]

    return json.dumps(score_info)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 9: update_verification
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def update_verification(
    entity_name: str,
    verification_outcome: str,
    usefulness: float,
    verification_context: str | None = None,
) -> str:
    """Update verification fields on your existing rating for a public entity.

    Use after actually testing/applying the knowledge in practice.

    Args:
        entity_name: Name of the entity
        verification_outcome: "confirmed", "contradicted", or "inconclusive"
        usefulness: How useful was the knowledge in practice? (0.0-1.0)
        verification_context: Description of how verification was done
    """
    if verification_outcome not in _VERIFICATION_OUTCOMES:
        return _error(f"verification_outcome must be one of {_VERIFICATION_OUTCOMES}")
    if not (0.0 <= usefulness <= 1.0):
        return _error(f"usefulness must be between 0.0 and 1.0, got {usefulness}")

    rater_id = os.environ.get("GITHUB_USER", socket.gethostname())

    with _get_conn() as conn:
        # Get current content hash
        result = _entity_content_hash(conn, entity_name)
        if not result:
            return _error(f"Entity '{entity_name}' not found or has no observations")
        c_hash = result[0]

        # Update existing rating
        cur = conn.execute(
            "UPDATE knowledge_ratings SET verification_outcome = ?, "
            "usefulness = ?, verification_context = ? "
            "WHERE entity_name = ? AND rater_id = ? AND content_hash = ?",
            (
                verification_outcome,
                usefulness,
                verification_context,
                entity_name,
                rater_id,
                c_hash,
            ),
        )
        if cur.rowcount == 0:
            return json.dumps(
                {
                    "error": "No existing rating found for this entity/version",
                    "hint": "You must rate_public_knowledge first before updating verification",
                }
            )

        score_info = _compute_truth_score(entity_name, conn)

    logger.info(
        "update_verification: %s by %s → %s (score=%.4f)",
        entity_name,
        rater_id,
        verification_outcome,
        score_info["truth_score"],
    )
    return json.dumps(
        {
            "status": "verification_updated",
            "entity_name": entity_name,
            "verification_outcome": verification_outcome,
            **score_info,
        }
    )


# ── Entry point ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run(transport="stdio")
