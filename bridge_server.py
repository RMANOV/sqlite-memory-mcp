#!/usr/bin/env python3
"""Thin MCP server exposing only bridge sync tools.

Shares the same SQLite database as the main sqlite-kb server and keeps the bridge
surface independently deployable; ``unified_server.py`` also mounts it.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from fastmcp_compat import FastMCP

from db_utils import (
    apply_task_mutation as _apply_task_mutation,
    json_loads as _json_loads,
    get_conn as _get_conn,
    create_task_with_ledger as _create_task_with_ledger,
    TaskDAO,
    _NOWIN,
    now_iso as _now,
    parse_iso_datetime_for_compare as _parse_iso_dt,
    sanitize_task_enums as _sanitize_task_enums,
    setup_logger,
    EXTENDED_MEMORY_KEYS as _EXTENDED_MEMORY_KEYS,  # noqa: F401
    BRIDGE_REPO,
    git_run as _git_run,
    get_last_bridge_auto_abort as _get_last_bridge_auto_abort,
    inspect_bridge_repo_blocker as _inspect_bridge_repo_blocker,
    validate_github_username as _validate_github_user,
)
from bridge_sync_worker import main as _bridge_sync_main
from schema import (
    error as _error,
    is_valid_timestamp as _is_valid_timestamp,
)
from runtime_parity import (
    collect_runtime_parity as _collect_runtime_parity,
    runtime_warning_summary as _runtime_warning_summary,
    write_runtime_parity_manifest as _write_runtime_parity_manifest,
)
from premium_runtime import maybe_mount_premium_extensions
from surface_contract import (
    build_surface_contract_report as _build_surface_contract_report,
)

# ── Logging (file-only, NEVER stdout — breaks MCP stdio) ────────────────

logger = setup_logger("sqlite-bridge", "bridge_server.log")


def _is_newer_timestamp(candidate_ts: str | None, baseline_ts: str | None) -> bool:
    return _parse_iso_dt(candidate_ts) > _parse_iso_dt(baseline_ts)


# ── FastMCP app ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-bridge",
    instructions=(
        "Bridge sync tools: cross-machine push/pull, task assignment, shared task review, "
        "recurring tasks, and bridge_doctor self-checks for runtime parity and surface contract. "
        "Shares DB with sqlite-kb."
    ),
)

# ── Bridge helpers ────────────────────────────────────────────────────────


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run git in BRIDGE_REPO. Thin wrapper around db_utils.git_run."""
    timeouts = {"pull": 120, "push": 300, "commit": 60}
    timeout = timeouts.get(args[0], 30) if args else 30
    return _git_run(BRIDGE_REPO, *args, timeout=timeout)


def _active_db_path() -> str:
    """Resolve the DB behind the server connection factory for worker delegation."""
    with _get_conn() as conn:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row["file"] if isinstance(row, sqlite3.Row) else row[2])


def _is_known_collaborator(
    conn: sqlite3.Connection,
    github_user: str | None,
    required_trust: str | None = None,
) -> bool:
    """Check collaborator membership, optionally enforcing a trust level."""
    if not github_user:
        return False
    if required_trust is None:
        row = conn.execute(
            "SELECT 1 FROM collaborators WHERE github_user = ?",
            (github_user,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM collaborators WHERE github_user = ? AND trust_level = ?",
            (github_user, required_trust),
        ).fetchone()
    return row is not None


# ═══════════════════════════════════════════════════════════════════════════
# Tool 1: bridge_push
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def bridge_push(tag: str = "shared", force: bool = False) -> str:
    """Run the canonical locked bridge worker and return the MCP result envelope."""
    if not Path(BRIDGE_REPO).is_dir():
        return _error(f"Bridge repo not found at {BRIDGE_REPO}")

    stats = _bridge_sync_main(
        db_path=_active_db_path(),
        bridge_repo=BRIDGE_REPO,
        force=force,
        entity_project_prefix=tag,
    )
    result = dict(stats)
    result["pushed_to_remote"] = bool(stats.get("pushed"))
    if any(
        key.startswith("blocked_by_") or key.endswith("_failed")
        for key, value in stats.items()
        if value is True
    ):
        message = str(stats.get("message") or "Bridge push was blocked")
        result.setdefault("error", message)
    return json.dumps(result)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 2: bridge_pull
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def bridge_pull() -> str:
    """Run the canonical locked bridge worker in import-only mode."""
    if not Path(BRIDGE_REPO).is_dir():
        return _error(f"Bridge repo not found at {BRIDGE_REPO}")

    stats = _bridge_sync_main(
        db_path=_active_db_path(),
        bridge_repo=BRIDGE_REPO,
        pull_only=True,
    )
    result = dict(stats)
    if any(
        key.startswith("blocked_by_") or key.endswith("_failed")
        for key, value in stats.items()
        if value is True
    ):
        message = str(stats.get("message") or "Bridge pull was blocked")
        result.setdefault("error", message)
    return json.dumps(result)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 3: bridge_status
# ═══════════════════════════════════════════════════════════════════════════
def _task_status_counts_from_db(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM tasks "
        "WHERE status NOT IN ('archived', 'cancelled') "
        "GROUP BY status ORDER BY status"
    ).fetchall()
    return {str(r["status"]): int(r["cnt"]) for r in rows}


def _task_status_counts_from_payload(payload: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in payload.get("tasks", []):
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _bridge_updated_at_churn_report(
    conn: sqlite3.Connection,
    *,
    min_cluster_size: int = 25,
    limit: int = 10,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT updated_at,
               COUNT(*) AS total,
               SUM(CASE WHEN status IN ('archived','cancelled') THEN 1 ELSE 0 END) AS hidden,
               SUM(CASE WHEN status NOT IN ('archived','cancelled') THEN 1 ELSE 0 END) AS exportable
        FROM tasks
        WHERE updated_at IS NOT NULL AND updated_at != ''
        GROUP BY updated_at
        HAVING total >= ?
        ORDER BY total DESC, updated_at DESC
        LIMIT ?
        """,
        (min_cluster_size, limit),
    ).fetchall()
    clusters = []
    for row in rows:
        total = int(row["total"] or 0)
        hidden = int(row["hidden"] or 0)
        clusters.append(
            {
                "updated_at": row["updated_at"],
                "total": total,
                "hidden": hidden,
                "exportable": int(row["exportable"] or 0),
                "suspicious": total >= min_cluster_size and hidden >= total // 2,
            }
        )
    return {
        "min_cluster_size": min_cluster_size,
        "clusters": clusters,
        "suspicious_count": sum(1 for c in clusters if c["suspicious"]),
    }


@mcp.tool()
def bridge_status() -> str:
    """Show bridge sync status — local shared entities vs repo contents."""
    if not Path(BRIDGE_REPO).is_dir():
        return _error(f"Bridge repo not found at {BRIDGE_REPO}")

    repo_blocker = _inspect_bridge_repo_blocker(BRIDGE_REPO)

    with _get_conn() as conn:
        local_rows = conn.execute(
            "SELECT name FROM entities WHERE project LIKE 'shared%' ORDER BY name"
        ).fetchall()
        local_task_count = TaskDAO.count_active(conn)
        local_task_status_counts = _task_status_counts_from_db(conn)

        # v0.6.0: collaboration stats
        collab_rows = conn.execute(
            "SELECT github_user, display_name, trust_level, last_sync_at "
            "FROM collaborators ORDER BY added_at"
        ).fetchall()
        pending_knowledge = conn.execute(
            "SELECT COUNT(*) as cnt FROM pending_shared_entities"
        ).fetchone()["cnt"]
        pending_rels = conn.execute(
            "SELECT COUNT(*) as cnt FROM pending_shared_relations"
        ).fetchone()["cnt"]
        sharing_rule_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM sharing_rules"
        ).fetchone()["cnt"]

        # v0.7.0: public knowledge counts
        public_ent_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM entities WHERE visibility='public'"
        ).fetchone()["cnt"]
        pending_pub_ent_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM entities WHERE visibility='pending_public'"
        ).fetchone()["cnt"]
        public_task_count = TaskDAO.count_by_visibility(conn, "public")
        pending_pub_task_count = TaskDAO.count_by_visibility(conn, "pending_public")

        # v0.9.0: rating statistics
        total_ratings = conn.execute(
            "SELECT COUNT(*) as cnt FROM knowledge_ratings"
        ).fetchone()["cnt"]
        rated_entities = conn.execute(
            "SELECT COUNT(DISTINCT entity_name) as cnt FROM knowledge_ratings"
        ).fetchone()["cnt"]
        anomaly_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM rating_anomalies WHERE resolved = 0"
        ).fetchone()["cnt"]
    local_names = {r["name"] for r in local_rows}

    if repo_blocker:
        return json.dumps(
            {
                "blocked_by_repo_state": True,
                "error": repo_blocker,
                "message": repo_blocker,
                "bridge_repo": BRIDGE_REPO,
                "remote_status": "suppressed_repo_blocked",
                "local_shared_count": len(local_names),
                "local_tasks": local_task_count,
                "local_task_status_counts": local_task_status_counts,
                "collaborators": [dict(r) for r in collab_rows],
                "collaborator_count": len(collab_rows),
                "pending_shared_knowledge": pending_knowledge,
                "pending_shared_relations": pending_rels,
                "sharing_rules": sharing_rule_count,
                "public_entities": public_ent_count,
                "pending_public_entities": pending_pub_ent_count,
                "public_tasks": public_task_count,
                "pending_public_tasks": pending_pub_task_count,
                "total_ratings": total_ratings,
                "rated_entities": rated_entities,
                "anomalies": anomaly_count,
            }
        )

    shared_path = Path(BRIDGE_REPO) / "shared.json"
    _status_eidx_path = Path(BRIDGE_REPO) / "entities_index.json"
    remote_names: set[str] = set()
    remote_task_count = 0
    remote_task_status_counts: dict[str, int] = {}
    repo_meta = {}
    # v4: entity names from entities_index.json (independent of shared.json)
    if _status_eidx_path.exists():
        try:
            _eidx = _json_loads(_status_eidx_path.read_text(encoding="utf-8"))
            remote_names = {e["name"] for e in _eidx.get("entities", []) if "name" in e}
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(
                "bridge_pull status: ignoring corrupt entities_index.json: %s", e
            )
    if shared_path.exists():
        try:
            payload = _json_loads(shared_path.read_text(encoding="utf-8"))
            if not remote_names:
                remote_names = {e["name"] for e in payload.get("entities", [])}
            remote_task_count = len(payload.get("tasks", []))
            remote_task_status_counts = _task_status_counts_from_payload(payload)
            repo_meta = {
                "pushed_at": payload.get("pushed_at"),
                "machine_id": payload.get("machine_id"),
                "version": payload.get("version"),
                "owner": payload.get("owner"),
            }
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("bridge_pull status: ignoring corrupt shared.json: %s", e)

    only_local = sorted(local_names - remote_names)
    only_remote = sorted(remote_names - local_names)
    in_sync = sorted(local_names & remote_names)

    # Git log for last push/pull timestamps
    log_result = _git("log", "-1", "--format=%ci %s")
    last_commit = log_result.stdout.strip() if log_result.returncode == 0 else None

    return json.dumps(
        {
            "local_shared_count": len(local_names),
            "remote_count": len(remote_names),
            "in_sync": len(in_sync),
            "only_local": only_local,
            "only_remote": only_remote,
            "local_tasks": local_task_count,
            "local_task_status_counts": local_task_status_counts,
            "remote_tasks": remote_task_count,
            "remote_task_status_counts": remote_task_status_counts,
            "task_count_delta": local_task_count - remote_task_count,
            "task_counts_match": local_task_count == remote_task_count,
            "last_commit": last_commit,
            "repo_meta": repo_meta,
            "collaborators": [dict(r) for r in collab_rows],
            "collaborator_count": len(collab_rows),
            "pending_shared_knowledge": pending_knowledge,
            "pending_shared_relations": pending_rels,
            "sharing_rules": sharing_rule_count,
            "public_entities": public_ent_count,
            "pending_public_entities": pending_pub_ent_count,
            "public_tasks": public_task_count,
            "pending_public_tasks": pending_pub_task_count,
            "total_ratings": total_ratings,
            "rated_entities": rated_entities,
            "anomalies": anomaly_count,
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 3b: bridge_doctor
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def bridge_doctor(write_manifest: bool = True) -> str:
    """Inspect runtime parity and the active bridge surface contract."""
    if write_manifest:
        parity = _write_runtime_parity_manifest()
    else:
        parity = _collect_runtime_parity()
    warning = _runtime_warning_summary(parity)
    repo_exists = Path(BRIDGE_REPO).is_dir()
    with _get_conn() as conn:
        updated_at_churn = _bridge_updated_at_churn_report(conn)
    return json.dumps(
        {
            "repo_exists": repo_exists,
            "bridge_repo": BRIDGE_REPO,
            "runtime_parity": parity,
            "runtime_warning": warning,
            "surface_contract": _build_surface_contract_report(),
            "updated_at_churn": updated_at_churn,
            "auto_abort_attempts": _get_last_bridge_auto_abort(),
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 4: assign_task
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def assign_task(task_id: str, assignee: str | None = None) -> str:
    """Assign a task or note to a GitHub user for collaboration.

    Sets assignee field. On next bridge_push, the item will be
    pushed to https://github.com/{assignee}/memory-bridge.
    Pass assignee=None to unassign.
    """
    if assignee is not None:
        try:
            _validate_github_user(assignee)
        except ValueError as exc:
            return _error(str(exc))

    now = _now()
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT id, assignee, shared_by FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not existing:
            return _error(f"Task {task_id} not found")

        shared_by = None
        if assignee:
            try:
                result = subprocess.run(
                    ["git", "config", "--global", "user.name"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    **_NOWIN,
                )
                shared_by = result.stdout.strip() or None
            except (subprocess.TimeoutExpired, OSError):
                pass

        result = _apply_task_mutation(
            conn,
            task_id,
            {"assignee": assignee, "shared_by": shared_by},
            timestamp=now,
            tool_name="bridge_server.assign_task",
            actor_type="user",
            actor_id=shared_by,
            source_kind="task",
            source_ref=task_id,
        )
        if result.get("missing"):
            return _error(f"Task {task_id} not found")

    action = f"assigned to {assignee}" if assignee else "unassigned"
    logger.info("assign_task: %s %s", task_id, action)
    return json.dumps(
        {"task_id": task_id, "assignee": assignee, "shared_by": shared_by}
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tool 5: review_shared_tasks
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def review_shared_tasks(
    action: str = "list",
    task_ids: list[str] | None = None,
) -> str:
    """Review shared tasks pending approval from other users.

    Shared tasks from bridge_pull are staged — never auto-imported.
    Use this tool to list, approve, or reject them.

    Args:
        action: list | approve | reject.
        task_ids: UUIDs to approve/reject. If None with approve/reject, applies to ALL pending.
    """
    if action not in ("list", "approve", "reject"):
        return _error("action must be: list, approve, reject")

    with _get_conn() as conn:
        if action == "list":
            rows = conn.execute(
                "SELECT id, title, type, priority, shared_by, received_at "
                "FROM pending_shared_tasks ORDER BY received_at DESC"
            ).fetchall()
            if not rows:
                return json.dumps(
                    {"pending": [], "count": 0, "message": "No pending shared tasks"}
                )
            items = [dict(r) for r in rows]
            return json.dumps({"pending": items, "count": len(items)})

        # Build WHERE for specific IDs or all
        if task_ids:
            ph = ",".join("?" * len(task_ids))
            where = f"id IN ({ph})"
            params = list(task_ids)
        else:
            where = "1=1"
            params = []

        if action == "approve":
            rows = conn.execute(
                f"SELECT * FROM pending_shared_tasks WHERE {where}", params
            ).fetchall()
            imported = 0
            for row in rows:
                t = dict(row)
                _sanitize_task_enums(t)
                tid = t["id"]
                existing = conn.execute(
                    "SELECT updated_at FROM tasks WHERE id = ?", (tid,)
                ).fetchone()
                if existing:
                    remote_ts = t.get("updated_at", "")
                    if _is_valid_timestamp(remote_ts) and _is_newer_timestamp(
                        remote_ts, existing["updated_at"]
                    ):
                        # Content protection: don't overwrite non-NULL local with NULL remote
                        local_content = conn.execute(
                            "SELECT description, notes FROM tasks WHERE id=?", (tid,)
                        ).fetchone()
                        safe_desc = (
                            t.get("description")
                            if t.get("description") is not None
                            else (
                                local_content["description"] if local_content else None
                            )
                        )
                        safe_notes = (
                            t.get("notes")
                            if t.get("notes") is not None
                            else (local_content["notes"] if local_content else None)
                        )
                        result = _apply_task_mutation(
                            conn,
                            tid,
                            {
                                "title": t["title"],
                                "description": safe_desc,
                                "status": t["status"],
                                "priority": t["priority"],
                                "section": t["section"],
                                "due_date": t.get("due_date"),
                                "project": t.get("project"),
                                "parent_id": t.get("parent_id"),
                                "notes": safe_notes,
                                "recurring": t.get("recurring"),
                                "type": t.get("type", "task"),
                                "assignee": t.get("assignee"),
                                "shared_by": t.get("shared_by"),
                            },
                            timestamp=t.get("updated_at", _now()),
                            tool_name="bridge_server.review_shared_tasks.approve",
                            actor_type="system",
                            source_kind="pending_shared_task",
                            source_ref=tid,
                        )
                        imported += int(result.get("updated", 0))
                else:
                    _create_task_with_ledger(
                        conn,
                        tid,
                        t["title"],
                        t["updated_at"],
                        description=t.get("description"),
                        status=t["status"],
                        priority=t["priority"],
                        section=t["section"],
                        due_date=t.get("due_date"),
                        project=t.get("project"),
                        parent_id=t.get("parent_id"),
                        notes=t.get("notes"),
                        recurring=t.get("recurring"),
                        type=t.get("type", "task"),
                        assignee=t.get("assignee"),
                        shared_by=t.get("shared_by"),
                        created_at=t.get("created_at"),
                        tool_name="bridge_server.review_shared_tasks.approve",
                        actor_type="system",
                        source_kind="pending_shared_task",
                        source_ref=tid,
                    )
                    imported += 1
                conn.execute("DELETE FROM pending_shared_tasks WHERE id = ?", (tid,))
            logger.info("review_shared_tasks: approved %d tasks", imported)
            return json.dumps({"approved": imported, "imported": imported})

        # action == "reject"
        cur = conn.execute(f"DELETE FROM pending_shared_tasks WHERE {where}", params)
        rejected = cur.rowcount
        logger.info("review_shared_tasks: rejected %d tasks", rejected)
        return json.dumps({"rejected": rejected})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 6: process_recurring_tasks
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def process_recurring_tasks(dry_run: bool = False) -> str:
    """Process recurring tasks: recreate done recurring tasks if schedule matches today.

    Finds tasks with status='done' and a recurring JSON config, checks if today
    matches the schedule, and creates a new not_started copy (idempotent — skips
    if an active task with the same title already exists).

    Args:
        dry_run: If True, show what would be created without inserting.
    """
    from recurring_tasks import process_recurring

    with _get_conn() as conn:
        created = process_recurring(conn, dry_run=dry_run)

    if not created:
        return json.dumps(
            {"message": "No recurring tasks to process today.", "created": 0}
        )

    titles = [t["title"] for t in created]
    prefix = "[dry-run] Would create" if dry_run else "Created"
    logger.info("process_recurring_tasks: %s %d task(s)", prefix.lower(), len(created))
    return json.dumps(
        {
            "message": f"{prefix} {len(created)} recurring task(s)",
            "created": len(created),
            "tasks": titles,
        }
    )


# ── Entry point ──────────────────────────────────────────────────────────
def main() -> None:
    maybe_mount_premium_extensions(mcp, server_name="sqlite-bridge")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
