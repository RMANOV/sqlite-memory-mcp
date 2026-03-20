#!/usr/bin/env python3
"""Thin MCP server exposing only bridge sync tools.

Shares the same SQLite database as the main sqlite-kb server.
Exists because Claude Code 2.x has a tool-count limit per MCP server
(~9 tools visible out of 50), so bridge tools are split into a separate server.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from db_utils import (
    json_dumps as _json_dumps,
    json_loads as _json_loads,
    get_conn as _get_conn,
    get_entity_id as _get_entity_id,
    fts_sync_entity as _fts_sync,
    TaskDAO,
    PUBLISH_STANDBY_MINUTES as _PUBLISH_STANDBY_MINUTES,
    MERGEABLE_FIELDS as _MERGEABLE_FIELDS,
    _NOWIN,
    now_iso as _now,
    sanitize_task_enums as _sanitize_task_enums,
    upsert_field_versions as _upsert_field_versions,
    merge_import_tasks as _merge_import_tasks,
    export_task_files as _export_task_files,
    export_index_json as _export_index_json,
    migrate_to_per_task_files as _migrate_to_per_task_files,
    BRIDGE_REPO,
)
from schema import (
    init_db,
    error as _error,
    clamp_score as _clamp_score,
    is_valid_timestamp as _is_valid_timestamp,
)

# ── DB schema init ────────────────────────────────────────────────────────
init_db()

# ── Logging (file-only, NEVER stdout — breaks MCP stdio) ────────────────
LOG_PATH = Path.home() / ".claude" / "memory" / "bridge_server.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("sqlite-bridge")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
if not logger.handlers:
    logger.addHandler(_fh)

# ── FastMCP app ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-bridge",
    instructions=(
        "Bridge sync tools: cross-machine push/pull, task assignment, shared task review, "
        "recurring tasks. Shares DB with sqlite-kb."
    ),
)

# ── Debounced bridge auto-sync ──────────────────────────────────────
_bridge_sync_timer: threading.Timer | None = None
_bridge_sync_lock = threading.Lock()
_BRIDGE_SYNC_DELAY = 60  # seconds, matches task_tray.py


def _schedule_bridge_sync():
    """Schedule a debounced bridge sync. Resets timer on each call."""
    global _bridge_sync_timer
    with _bridge_sync_lock:
        if _bridge_sync_timer is not None:
            _bridge_sync_timer.cancel()
        _bridge_sync_timer = threading.Timer(_BRIDGE_SYNC_DELAY, _run_bridge_sync)
        _bridge_sync_timer.daemon = True  # don't block process exit
        _bridge_sync_timer.start()


def _run_bridge_sync():
    """Execute bridge sync in background thread."""
    global _bridge_sync_timer
    try:
        import bridge_sync_worker

        stats = bridge_sync_worker.main()
        logger.info("auto-sync: %s", stats)
    except Exception as exc:
        logger.warning("auto-sync failed: %s", exc)
    finally:
        with _bridge_sync_lock:
            _bridge_sync_timer = None


# ── Bridge helpers ────────────────────────────────────────────────────────


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run a git command in the bridge repo. Never prints to stdout."""
    result = subprocess.run(
        ["git", "-C", BRIDGE_REPO, *args],
        capture_output=True,
        text=True,
        timeout=30,
        **_NOWIN,
    )
    if result.returncode != 0:
        logger.warning("git %s failed: %s", " ".join(args), result.stderr.strip())
    return result


_GITHUB_USER_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,37}[a-zA-Z0-9])?$")


def _validate_github_user(username: str) -> None:
    """Raise ValueError if username is not a valid GitHub username."""
    if not _GITHUB_USER_RE.match(username):
        raise ValueError(f"Invalid GitHub username: {username!r}")


def _push_to_assignee(assignee: str, tasks: list[dict]) -> None:
    """Push assigned tasks to another user's memory-bridge repo."""
    import tempfile

    _validate_github_user(assignee)
    repo_url = f"https://github.com/{assignee}/memory-bridge.git"
    with tempfile.TemporaryDirectory() as tmpdir:
        clone = subprocess.run(
            ["git", "clone", "--depth=1", repo_url, tmpdir],
            capture_output=True,
            text=True,
            timeout=30,
            **_NOWIN,
        )
        if clone.returncode != 0:
            logger.warning(
                "_push_to_assignee: clone failed for %s: %s",
                assignee,
                clone.stderr.strip(),
            )
            return

        shared_path = Path(tmpdir) / "shared.json"
        existing: dict = {}
        if shared_path.exists():
            try:
                existing = json.loads(shared_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # Merge into shared_tasks array (upsert by id, last-write-wins)
        shared_tasks = {t["id"]: t for t in existing.get("shared_tasks", [])}
        for t in tasks:
            if t.get("updated_at", "") >= shared_tasks.get(t["id"], {}).get(
                "updated_at", ""
            ):
                shared_tasks[t["id"]] = t
        existing["shared_tasks"] = list(shared_tasks.values())

        tmp_path = shared_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp_path, shared_path)

        subprocess.run(
            ["git", "-C", tmpdir, "add", "shared.json"],
            capture_output=True,
            timeout=10,
            **_NOWIN,
        )
        hostname = socket.gethostname()
        msg = f"bridge: shared {len(tasks)} tasks from {hostname} to {assignee}"
        commit = subprocess.run(
            ["git", "-C", tmpdir, "commit", "-m", msg],
            capture_output=True,
            text=True,
            timeout=10,
            **_NOWIN,
        )
        if commit.returncode == 0:
            push = subprocess.run(
                ["git", "-C", tmpdir, "push"],
                capture_output=True,
                text=True,
                timeout=30,
                **_NOWIN,
            )
            if push.returncode == 0:
                logger.info(
                    "_push_to_assignee: pushed %d tasks to %s", len(tasks), assignee
                )
            else:
                logger.warning(
                    "_push_to_assignee: push failed for %s: %s",
                    assignee,
                    push.stderr.strip(),
                )


def _source_hash(name: str, entity_type: str, observations: list) -> str:
    """SHA256 hash for deduplication of shared entities."""
    raw = json.dumps({"n": name, "t": entity_type, "o": observations}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _push_knowledge_to(conn: sqlite3.Connection, target_user: str) -> int:
    """Push shared knowledge (entities + relations) to a collaborator's repo."""
    import tempfile

    # Gather entities to share based on sharing_rules
    rules = conn.execute(
        "SELECT entity_name, share_type, priority FROM sharing_rules WHERE target_user IN (?, '*')",
        (target_user,),
    ).fetchall()
    if not rules:
        return 0

    entity_names: set[str] = set()
    include_relations = False
    priorities: dict[str, str] = {}  # entity_name → priority
    for r in rules:
        if r["share_type"] in ("entity", "all"):
            if r["entity_name"] == "*":
                # All shared-tagged entities
                rows = conn.execute(
                    "SELECT name FROM entities WHERE project LIKE 'shared%'"
                ).fetchall()
                for row in rows:
                    entity_names.add(row["name"])
                    priorities[row["name"]] = r["priority"]
            else:
                entity_names.add(r["entity_name"])
                priorities[r["entity_name"]] = r["priority"]
        if r["share_type"] in ("relation", "all"):
            include_relations = True

    if not entity_names:
        return 0

    # Build knowledge payload
    knowledge_out = []
    entity_ids = set()
    for ename in entity_names:
        erow = conn.execute(
            "SELECT id, name, entity_type, project FROM entities WHERE name = ?",
            (ename,),
        ).fetchone()
        if not erow:
            continue
        entity_ids.add(erow["id"])
        obs = conn.execute(
            "SELECT content, created_at FROM observations WHERE entity_id = ? ORDER BY id",
            (erow["id"],),
        ).fetchall()
        obs_list = [
            {"content": o["content"], "createdAt": o["created_at"]} for o in obs
        ]
        entry = {
            "name": erow["name"],
            "entityType": erow["entity_type"],
            "project": erow["project"],
            "observations": obs_list,
            "priority": priorities.get(ename, "medium"),
            "sharedBy": os.environ.get("GITHUB_USER", socket.gethostname()),
            "sharedAt": _now(),
            "sourceHash": _source_hash(erow["name"], erow["entity_type"], obs_list),
        }
        # Attach relations if requested
        if include_relations:
            rels = conn.execute(
                "SELECT et.name AS to_name, r.relation_type "
                "FROM relations r JOIN entities et ON r.to_id = et.id "
                "WHERE r.from_id = ?",
                (erow["id"],),
            ).fetchall()
            entry["relations"] = [
                {"to": r["to_name"], "relationType": r["relation_type"]}
                for r in rels
                if r["to_name"] in entity_names
            ]
        knowledge_out.append(entry)

    if not knowledge_out:
        return 0

    # Clone target repo, merge knowledge, push
    _validate_github_user(target_user)
    repo_url = f"https://github.com/{target_user}/memory-bridge.git"
    with tempfile.TemporaryDirectory() as tmpdir:
        clone = subprocess.run(
            ["git", "clone", "--depth=1", repo_url, tmpdir],
            capture_output=True,
            text=True,
            timeout=30,
            **_NOWIN,
        )
        if clone.returncode != 0:
            logger.warning(
                "_push_knowledge_to: clone failed for %s: %s",
                target_user,
                clone.stderr.strip(),
            )
            return 0

        shared_path = Path(tmpdir) / "shared.json"
        existing: dict = {}
        if shared_path.exists():
            try:
                existing = json.loads(shared_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # Merge into shared_knowledge (dedup by sourceHash)
        current = {e["sourceHash"]: e for e in existing.get("shared_knowledge", [])}
        for entry in knowledge_out:
            current[entry["sourceHash"]] = entry
        existing["shared_knowledge"] = list(current.values())

        tmp_path = shared_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp_path, shared_path)

        subprocess.run(
            ["git", "-C", tmpdir, "add", "shared.json"],
            capture_output=True,
            timeout=10,
            **_NOWIN,
        )
        hostname = socket.gethostname()
        msg = f"bridge: shared {len(knowledge_out)} entities from {hostname} to {target_user}"
        commit = subprocess.run(
            ["git", "-C", tmpdir, "commit", "-m", msg],
            capture_output=True,
            text=True,
            timeout=10,
            **_NOWIN,
        )
        if commit.returncode == 0:
            push = subprocess.run(
                ["git", "-C", tmpdir, "push"],
                capture_output=True,
                text=True,
                timeout=30,
                **_NOWIN,
            )
            if push.returncode == 0:
                logger.info(
                    "_push_knowledge_to: pushed %d entities to %s",
                    len(knowledge_out),
                    target_user,
                )
                return len(knowledge_out)
            else:
                logger.warning(
                    "_push_knowledge_to: push failed for %s: %s",
                    target_user,
                    push.stderr.strip(),
                )
        return 0


# ═══════════════════════════════════════════════════════════════════════════
# Tool 1: bridge_push
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def bridge_push(tag: str = "shared", force: bool = False) -> str:
    """Push tagged entities to the bridge git repo for cross-machine sync.

    Exports entities where project LIKE '{tag}%' with their observations
    and inter-relations to JSON. Git add, commit, push.

    Incremental: skips full export if nothing changed since last push.
    Set force=True to push regardless.
    """
    if not Path(BRIDGE_REPO).is_dir():
        return json.dumps(
            {
                "error": f"Bridge repo not found at {BRIDGE_REPO}. "
                "Run: mkdir -p {BRIDGE_REPO} && git -C {BRIDGE_REPO} init"
            }
        )

    # v2.0.0: Pull before push (prevents overwriting remote changes)
    pull_result = _git("pull", "--rebase", "--autostash")
    if pull_result.returncode != 0:
        logger.warning("bridge_push: git pull failed: %s", pull_result.stderr.strip())

    # v2.0.0: One-time migration shared.json → per-task files
    _migrate_to_per_task_files(BRIDGE_REPO)

    with _get_conn() as conn:
        # v2.0.0: LWW merge remote index.json into local DB
        _bp_index_path = Path(BRIDGE_REPO) / "index.json"
        if _bp_index_path.exists():
            try:
                _remote_idx = _json_loads(_bp_index_path.read_text(encoding="utf-8"))
                _merge_import_tasks(conn, _remote_idx.get("tasks", []))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("bridge_push: index.json merge failed: %s", exc)

        # Incremental check: skip if no changes since last push
        if not force:
            last_push_row = conn.execute(
                "SELECT value FROM bridge_meta WHERE key = 'last_push_at'"
            ).fetchone()
            if last_push_row:
                last_push_at = last_push_row["value"]
                row = conn.execute(
                    "SELECT "
                    "  (SELECT COUNT(*) FROM tasks WHERE updated_at > ?) AS changed_tasks, "
                    "  (SELECT COUNT(*) FROM entities WHERE updated_at > ?) AS changed_ents, "
                    "  (SELECT COUNT(*) FROM entities WHERE visibility = 'pending_public') AS pending_pub",
                    (last_push_at, last_push_at),
                ).fetchone()
                if (
                    row["changed_tasks"] == 0
                    and row["changed_ents"] == 0
                    and row["pending_pub"] == 0
                ):
                    logger.info(
                        "bridge_push: no changes since %s, skipping", last_push_at
                    )
                    return json.dumps(
                        {
                            "pushed": 0,
                            "message": f"No changes since {last_push_at}. Use force=True to push anyway.",
                        }
                    )
        # v0.7.0: Promote pending_public → public if standby elapsed
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=_PUBLISH_STANDBY_MINUTES)
        ).isoformat()
        promoted_ent = conn.execute(
            "UPDATE entities SET visibility='public' "
            "WHERE visibility='pending_public' AND publish_requested_at <= ?",
            (cutoff,),
        ).rowcount
        promoted_tasks = TaskDAO.promote_pending_public(conn, cutoff)
        if promoted_ent or promoted_tasks:
            logger.info(
                "bridge_push: promoted %d entities, %d tasks to public",
                promoted_ent,
                promoted_tasks,
            )

        ent_rows = conn.execute(
            "SELECT id, name, entity_type, project, created_at, updated_at "
            "FROM entities WHERE project LIKE ? ORDER BY name",
            (f"{tag}%",),
        ).fetchall()

        entities_out = []
        entity_ids = set()
        for e in ent_rows:
            entity_ids.add(e["id"])
            obs = conn.execute(
                "SELECT content, created_at FROM observations "
                "WHERE entity_id = ? ORDER BY id",
                (e["id"],),
            ).fetchall()
            entities_out.append(
                {
                    "name": e["name"],
                    "entityType": e["entity_type"],
                    "project": e["project"],
                    "observations": [
                        {"content": o["content"], "createdAt": o["created_at"]}
                        for o in obs
                    ],
                    "createdAt": e["created_at"],
                    "updatedAt": e["updated_at"],
                }
            )

        # Relations where BOTH endpoints are in the shared set
        relations_out = []
        if entity_ids:
            ph = ",".join("?" * len(entity_ids))
            ids = list(entity_ids)
            rel_rows = conn.execute(
                f"SELECT ef.name AS from_name, et.name AS to_name, r.relation_type, r.created_at "
                f"FROM relations r "
                f"JOIN entities ef ON r.from_id = ef.id "
                f"JOIN entities et ON r.to_id = et.id "
                f"WHERE r.from_id IN ({ph}) AND r.to_id IN ({ph})",
                ids + ids,
            ).fetchall()
            relations_out = [
                {
                    "from": r["from_name"],
                    "to": r["to_name"],
                    "relationType": r["relation_type"],
                    "createdAt": r["created_at"],
                }
                for r in rel_rows
            ]

        # Export all non-archived tasks for cross-machine sync
        task_rows = conn.execute(
            "SELECT id, title, description, status, priority, section, due_date, "
            "project, parent_id, notes, recurring, type, assignee, shared_by, "
            "created_at, updated_at "
            "FROM tasks WHERE status NOT IN ('archived', 'cancelled') ORDER BY created_at"
        ).fetchall()
        tasks_out = [dict(r) for r in task_rows]

        # v2.0.0: Export per-task files + index.json
        last_push_at = None
        lp_row = conn.execute(
            "SELECT value FROM bridge_meta WHERE key = 'last_push_at'"
        ).fetchone()
        if lp_row:
            last_push_at = lp_row["value"]
        _export_task_files(conn, BRIDGE_REPO, changed_since=last_push_at)
        _export_index_json(conn, BRIDGE_REPO)

        # v0.7.0: Export public entities + tasks as public_knowledge
        pub_ent_rows = conn.execute(
            "SELECT id, name, entity_type, project, created_at, updated_at "
            "FROM entities WHERE visibility='public' ORDER BY name"
        ).fetchall()
        public_entities_out = []
        for pe in pub_ent_rows:
            obs = conn.execute(
                "SELECT content, created_at FROM observations "
                "WHERE entity_id = ? ORDER BY id",
                (pe["id"],),
            ).fetchall()
            public_entities_out.append(
                {
                    "name": pe["name"],
                    "entityType": pe["entity_type"],
                    "project": pe["project"],
                    "observations": [
                        {"content": o["content"], "createdAt": o["created_at"]}
                        for o in obs
                    ],
                    "createdAt": pe["created_at"],
                    "updatedAt": pe["updated_at"],
                }
            )
        pub_task_rows = conn.execute(
            "SELECT id, title, description, status, priority, section, "
            "due_date, project, created_at, updated_at "
            "FROM tasks WHERE visibility='public' ORDER BY created_at"
        ).fetchall()
        public_tasks_out = [dict(r) for r in pub_task_rows]

        # Build team_manifest from collaborators (same connection)
        collab_rows = conn.execute(
            "SELECT github_user FROM collaborators ORDER BY added_at"
        ).fetchall()
        collaborator_list = [r["github_user"] for r in collab_rows]

    hostname = socket.gethostname()
    owner = os.environ.get("GITHUB_USER", hostname)
    payload = {
        "version": 3,
        "pushed_at": _now(),
        "machine_id": hostname,
        "owner": owner,
        "entities": entities_out,
        "relations": relations_out,
        "tasks": tasks_out,
        "team_manifest": {
            "collaborators": collaborator_list,
            "display_name": owner,
        },
    }

    # v0.7.0: Add public_knowledge to payload
    if public_entities_out or public_tasks_out:
        payload["public_knowledge"] = {
            "entities": public_entities_out,
            "tasks": public_tasks_out,
        }

    # v0.9.0: Export knowledge_ratings
    with _get_conn() as conn:
        rating_rows = conn.execute(
            "SELECT entity_name, rater_id, content_hash, specificity, falsifiability, "
            "internal_consistency, novelty, verification_outcome, usefulness, "
            "verification_context, rated_at FROM knowledge_ratings ORDER BY rated_at"
        ).fetchall()
    if rating_rows:
        payload["knowledge_ratings"] = [dict(r) for r in rating_rows]

    # Merge remote tasks + preserve extra keys from remote
    shared_path = Path(BRIDGE_REPO) / "shared.json"
    index_exists = (Path(BRIDGE_REPO) / "index.json").exists()
    if shared_path.exists():
        try:
            existing = _json_loads(shared_path.read_text(encoding="utf-8"))

            if not index_exists:
                # Legacy merge: keep remote tasks that don't exist locally (by id)
                local_ids = {t["id"] for t in tasks_out}
                remote_tasks = existing.get("tasks", [])
                merged_count = 0
                for rt in remote_tasks:
                    if rt.get("id") and rt["id"] not in local_ids:
                        tasks_out.append(rt)
                        local_ids.add(rt["id"])
                        merged_count += 1
                if merged_count:
                    payload["tasks"] = tasks_out
                    logger.info(
                        "bridge_push: merged %d remote-only tasks into payload",
                        merged_count,
                    )

                # Update existing tasks where remote has newer updated_at
                local_by_id = {t["id"]: t for t in tasks_out}
                updated_count = 0
                for rt in remote_tasks:
                    rt_id = rt.get("id")
                    if not rt_id or rt_id not in local_by_id:
                        continue
                    lt = local_by_id[rt_id]
                    r_upd = rt.get("updated_at", "")
                    l_upd = lt.get("updated_at", "")
                    if r_upd > l_upd:
                        _sanitize_task_enums(rt)
                        for field in (
                            "status",
                            "section",
                            "priority",
                            "due_date",
                            "notes",
                            "description",
                            "type",
                        ):
                            if rt.get(field) is not None:
                                lt[field] = rt[field]
                        lt["updated_at"] = r_upd
                        updated_count += 1
                if updated_count:
                    logger.info(
                        "bridge_push: updated %d tasks from newer remote data",
                        updated_count,
                    )

            # Preserve extra keys (e.g. reading_tasks, shared_knowledge)
            known_keys = {
                "version",
                "pushed_at",
                "machine_id",
                "owner",
                "entities",
                "relations",
                "tasks",
                "shared_tasks",
                "shared_knowledge",
                "public_knowledge",
                "knowledge_ratings",
                "team_manifest",
            }
            for key, val in existing.items():
                if key not in known_keys and isinstance(val, (list, dict)):
                    payload[key] = val
                    logger.info(
                        "bridge_push: preserving extra key '%s' (%s)",
                        key,
                        f"{len(val)} items" if isinstance(val, list) else "dict",
                    )
        except (json.JSONDecodeError, OSError):
            pass

    tmp_path = shared_path.with_suffix(".tmp")
    tmp_path.write_text(_json_dumps(payload), encoding="utf-8")
    os.replace(tmp_path, shared_path)

    # Cross-account push: send assigned tasks to other users' repos
    by_assignee: dict[str, list] = {}
    for t in tasks_out:
        if t.get("assignee"):
            by_assignee.setdefault(t["assignee"], []).append(t)

    for target_user, assigned_tasks in by_assignee.items():
        try:
            _push_to_assignee(target_user, assigned_tasks)
        except Exception as exc:
            logger.warning("bridge_push: failed to push to %s: %s", target_user, exc)

    # Cross-account knowledge push: sharing_rules → collaborator repos
    # Phase 1: collect targets inside short transaction (release WAL quickly)
    knowledge_pushed = 0
    push_targets: list[str] = []
    with _get_conn() as conn:
        rules = conn.execute(
            "SELECT DISTINCT target_user FROM sharing_rules"
        ).fetchall()
        for rule_row in rules:
            target = rule_row["target_user"]
            collab = conn.execute(
                "SELECT trust_level FROM collaborators WHERE github_user = ?",
                (target,),
            ).fetchone()
            if collab:
                push_targets.append(target)

    # Phase 2: git operations outside transaction (no WAL lock during network I/O)
    successful_targets: list[str] = []
    for target in push_targets:
        try:
            with _get_conn() as conn:
                pushed_n = _push_knowledge_to(conn, target)
            knowledge_pushed += pushed_n
            successful_targets.append(target)
        except Exception as exc:
            logger.warning("bridge_push: knowledge push to %s failed: %s", target, exc)

    # Phase 3: update sync timestamps in short transaction
    if successful_targets:
        with _get_conn() as conn:
            now = _now()
            for target in successful_targets:
                conn.execute(
                    "UPDATE collaborators SET last_sync_at = ? WHERE github_user = ?",
                    (now, target),
                )

    n_obs = sum(len(e["observations"]) for e in entities_out)
    msg = (
        f"bridge: push {len(entities_out)} entities, "
        f"{len(tasks_out)} tasks from {hostname}"
    )

    _git("add", "shared.json", "index.json", "tasks/")
    # Use --porcelain to check staged changes without locale-dependent text parsing
    status_result = _git("status", "--porcelain")
    if not status_result.stdout.strip():
        logger.info("bridge_push: no changes to commit")
        return json.dumps({"pushed": 0, "message": "No changes — already up to date"})
    commit_result = _git("commit", "-m", msg)
    if commit_result.returncode != 0:
        logger.error("bridge_push: commit failed: %s", commit_result.stderr)
        return _error(f"git commit failed: {commit_result.stderr.strip()}")

    push_result = _git("push")
    pushed = push_result.returncode == 0

    logger.info(
        "bridge_push: %d entities, %d observations, %d relations, %d tasks, push=%s",
        len(entities_out),
        n_obs,
        len(relations_out),
        len(tasks_out),
        pushed,
    )
    result: dict[str, Any] = {
        "entities": len(entities_out),
        "observations": n_obs,
        "relations": len(relations_out),
        "tasks": len(tasks_out),
        "pushed_to_remote": pushed,
        "message": msg,
    }
    if knowledge_pushed:
        result["knowledge_shared"] = knowledge_pushed
    if promoted_ent or promoted_tasks:
        result["promoted_to_public"] = {
            "entities": promoted_ent,
            "tasks": promoted_tasks,
        }

    # v0.7.0: Create GitHub release when public_knowledge is pushed
    has_public = bool(public_entities_out or public_tasks_out)
    if pushed and has_public:
        n_pub_ent = len(public_entities_out)
        n_pub_tasks = len(public_tasks_out)
        tag_name = f"public-v{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        release_title = f"Public Knowledge: {n_pub_ent} entities, {n_pub_tasks} tasks"
        release_notes = (
            f"## Public Knowledge Release\n\n"
            f"- **{n_pub_ent}** public entities\n"
            f"- **{n_pub_tasks}** public tasks\n\n"
            f"Published from `{hostname}` at {_now()}"
        )
        try:
            rel_result = subprocess.run(
                [
                    "gh",
                    "release",
                    "create",
                    tag_name,
                    "--repo",
                    os.environ.get("BRIDGE_GH_REPO", "RMANOV/sqlite-memory-mcp"),
                    "--title",
                    release_title,
                    "--notes",
                    release_notes,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                **_NOWIN,
            )
            if rel_result.returncode == 0:
                result["github_release"] = tag_name
                logger.info("bridge_push: created GitHub release %s", tag_name)
            else:
                logger.warning(
                    "bridge_push: GitHub release failed: %s", rel_result.stderr.strip()
                )
        except Exception as exc:
            logger.warning("bridge_push: GitHub release error: %s", exc)

    if has_public:
        result["public_knowledge"] = {
            "entities": len(public_entities_out),
            "tasks": len(public_tasks_out),
        }

    if pushed:
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bridge_meta(key, value) VALUES('last_push_at', ?)",
                (_now(),),
            )

    return json.dumps(result)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 2: bridge_pull
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def bridge_pull() -> str:
    """Pull shared entities from the bridge git repo into local memory.

    Git pull, read shared.json, import new entities/observations/relations.
    UNIQUE constraints handle deduplication automatically.
    """
    if not Path(BRIDGE_REPO).is_dir():
        return _error(f"Bridge repo not found at {BRIDGE_REPO}")

    pull_result = _git("pull", "--rebase", "--autostash")
    git_pull_failed = pull_result.returncode != 0
    if git_pull_failed:
        logger.warning("bridge_pull: git pull failed, proceeding with local copy")

    shared_path = Path(BRIDGE_REPO) / "shared.json"
    _pull_index_path = Path(BRIDGE_REPO) / "index.json"
    _has_index = _pull_index_path.exists()

    if not shared_path.exists() and not _has_index:
        return _error("No sync data found in bridge repo")

    # Read shared.json for entities/relations (and legacy task fallback)
    payload: dict = {}
    if shared_path.exists():
        try:
            payload = _json_loads(shared_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            if not _has_index:
                return _error(f"Failed to read shared.json: {exc}")
            logger.warning("bridge_pull: shared.json parse failed: %s", exc)

    entities = payload.get("entities", [])
    relations = payload.get("relations", [])
    # Stage shared_tasks for review (never auto-import from other accounts)
    shared_tasks = payload.get("shared_tasks", [])
    staged_count = 0
    now = _now()
    new_entities = 0
    new_observations = 0
    new_relations = 0
    new_tasks = 0
    updated_tasks = 0

    with _get_conn() as conn:
        for ent in entities:
            cur = conn.execute(
                "INSERT OR IGNORE INTO entities "
                "(name, entity_type, project, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ent["name"],
                    ent["entityType"],
                    ent.get("project"),
                    ent.get("createdAt", now),
                    ent.get("updatedAt", now),
                ),
            )
            new_entities += cur.rowcount

            eid = _get_entity_id(conn, ent["name"])
            if eid:
                for obs in ent.get("observations", []):
                    content = obs["content"] if isinstance(obs, dict) else obs
                    created = (
                        obs.get("createdAt", now) if isinstance(obs, dict) else now
                    )
                    cur2 = conn.execute(
                        "INSERT OR IGNORE INTO observations "
                        "(entity_id, content, created_at) VALUES (?, ?, ?)",
                        (eid, content, created),
                    )
                    new_observations += cur2.rowcount
                _fts_sync(conn, eid)

        for rel in relations:
            from_id = _get_entity_id(conn, rel["from"])
            to_id = _get_entity_id(conn, rel["to"])
            if from_id and to_id:
                cur3 = conn.execute(
                    "INSERT OR IGNORE INTO relations "
                    "(from_id, to_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
                    (
                        from_id,
                        to_id,
                        rel["relationType"],
                        rel.get("createdAt", now),
                    ),
                )
                new_relations += cur3.rowcount

        # v2.0.0: Import tasks via per-field LWW merge from index.json
        if _has_index:
            try:
                _idx_data = _json_loads(_pull_index_path.read_text(encoding="utf-8"))
                new_tasks, updated_tasks = _merge_import_tasks(
                    conn, _idx_data.get("tasks", [])
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("bridge_pull: index.json read failed: %s", exc)
                new_tasks, updated_tasks = 0, 0
        else:
            # Legacy fallback: task-level LWW from shared.json
            tasks = list(payload.get("tasks", []))
            for key, val in payload.items():
                if (
                    key.endswith("_tasks")
                    and key != "tasks"
                    and key != "shared_tasks"
                    and isinstance(val, list)
                ):
                    tasks.extend(val)
            tasks_sorted = sorted(
                tasks,
                key=lambda t: (
                    t.get("parent_id") is not None,
                    t.get("created_at", ""),
                ),
            )
            for task in tasks_sorted:
                tid = task.get("id")
                if not tid:
                    continue
                _sanitize_task_enums(task)
                existing = conn.execute(
                    "SELECT updated_at FROM tasks WHERE id = ?", (tid,)
                ).fetchone()
                if existing:
                    if task.get("updated_at", "") > existing["updated_at"]:
                        conn.execute(
                            "UPDATE tasks SET title=?, description=?, status=?, "
                            "priority=?, section=?, due_date=?, project=?, "
                            "parent_id=?, notes=?, recurring=?, type=?, "
                            "assignee=?, shared_by=?, updated_at=? WHERE id=?",
                            (
                                task["title"],
                                task.get("description"),
                                task["status"],
                                task["priority"],
                                task["section"],
                                task.get("due_date"),
                                task.get("project"),
                                task.get("parent_id"),
                                task.get("notes"),
                                task.get("recurring"),
                                task.get("type", "task"),
                                task.get("assignee"),
                                task.get("shared_by"),
                                task["updated_at"],
                                tid,
                            ),
                        )
                        _upsert_field_versions(
                            conn, tid, _MERGEABLE_FIELDS, task.get("updated_at", now)
                        )
                        updated_tasks += 1
                else:
                    TaskDAO.create(
                        conn,
                        tid,
                        task["title"],
                        task.get("updated_at", now),
                        description=task.get("description"),
                        status=task["status"],
                        priority=task["priority"],
                        section=task["section"],
                        due_date=task.get("due_date"),
                        project=task.get("project"),
                        parent_id=task.get("parent_id"),
                        notes=task.get("notes"),
                        recurring=task.get("recurring"),
                        type=task.get("type", "task"),
                        assignee=task.get("assignee"),
                        shared_by=task.get("shared_by"),
                        created_at=task.get("created_at", now),
                    )
                    new_tasks += 1

        # Stage shared_tasks for manual review (security: never auto-import)
        for st in shared_tasks:
            sid = st.get("id")
            if not sid:
                continue
            _sanitize_task_enums(st)
            conn.execute(
                "INSERT OR REPLACE INTO pending_shared_tasks "
                "(id, title, description, status, priority, section, due_date, "
                "project, parent_id, notes, recurring, type, assignee, shared_by, "
                "created_at, updated_at, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    st.get("title", "Untitled"),
                    st.get("description"),
                    st.get("status", "not_started"),
                    st.get("priority", "medium"),
                    st.get("section", "inbox"),
                    st.get("due_date"),
                    st.get("project"),
                    st.get("parent_id"),
                    st.get("notes"),
                    st.get("recurring"),
                    st.get("type", "task"),
                    st.get("assignee"),
                    st.get("shared_by"),
                    st.get("created_at", now),
                    st.get("updated_at", now),
                    now,
                ),
            )
            staged_count += 1

        # Stage shared_knowledge for review (v0.6.0 P2P knowledge collaboration)
        shared_knowledge = payload.get("shared_knowledge", [])
        staged_knowledge = 0
        staged_relations = 0
        for sk in shared_knowledge:
            sname = sk.get("name")
            if not sname:
                continue
            obs_json = json.dumps(sk.get("observations", []), ensure_ascii=False)
            shash = sk.get("sourceHash") or _source_hash(
                sname, sk.get("entityType", ""), sk.get("observations", [])
            )
            sender = sk.get("sharedBy", "unknown")

            # Check trust: only accept from known read_write collaborators
            collab = conn.execute(
                "SELECT trust_level FROM collaborators WHERE github_user = ?",
                (sender,),
            ).fetchone()
            if not collab or collab["trust_level"] != "read_write":
                logger.info(
                    "bridge_pull: skipping knowledge from untrusted sender %s", sender
                )
                continue

            conn.execute(
                "INSERT OR IGNORE INTO pending_shared_entities "
                "(name, entity_type, project, observations, priority, "
                "shared_by, source_hash, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sname,
                    sk.get("entityType", "unknown"),
                    sk.get("project"),
                    obs_json,
                    sk.get("priority", "medium"),
                    sender,
                    shash,
                    now,
                ),
            )
            staged_knowledge += 1

            # Stage relations if included
            for rel in sk.get("relations", []):
                conn.execute(
                    "INSERT OR IGNORE INTO pending_shared_relations "
                    "(from_entity, to_entity, relation_type, shared_by, received_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sname, rel["to"], rel["relationType"], sender, now),
                )
                staged_relations += 1

        # v0.7.0: Stage incoming public_knowledge from collaborators
        staged_public = 0
        public_knowledge = payload.get("public_knowledge", {})
        pk_entities = (
            public_knowledge.get("entities", [])
            if isinstance(public_knowledge, dict)
            else []
        )
        source_owner = payload.get("owner", "unknown")
        for pk in pk_entities:
            pname = pk.get("name")
            if not pname:
                continue
            obs_json = json.dumps(pk.get("observations", []), ensure_ascii=False)
            phash = _source_hash(
                pname, pk.get("entityType", ""), pk.get("observations", [])
            )
            conn.execute(
                "INSERT OR IGNORE INTO pending_shared_entities "
                "(name, entity_type, project, observations, priority, "
                "shared_by, source_hash, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pname,
                    pk.get("entityType", "unknown"),
                    pk.get("project"),
                    obs_json,
                    "medium",
                    f"public:{source_owner}",
                    phash,
                    now,
                ),
            )
            staged_public += 1
        if staged_public:
            logger.info(
                "bridge_pull: staged %d public knowledge entities for review",
                staged_public,
            )

        # v0.9.0: Import knowledge ratings with anti-gaming validation
        imported_ratings = 0
        local_owner = os.environ.get("GITHUB_USER", socket.gethostname())
        for kr in payload.get("knowledge_ratings", []):
            kr_rater = kr.get("rater_id", "")
            kr_entity = kr.get("entity_name", "")
            # Skip own ratings (don't import back)
            if kr_rater == local_owner:
                continue
            # Skip if entity doesn't exist locally or isn't public
            ent = conn.execute(
                "SELECT visibility FROM entities WHERE name = ?", (kr_entity,)
            ).fetchone()
            if not ent or ent["visibility"] != "public":
                continue
            # Validate content_hash is non-empty (required for rating integrity)
            c_hash = kr.get("content_hash", "")
            if not c_hash:
                continue
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO knowledge_ratings "
                    "(entity_name, rater_id, content_hash, specificity, falsifiability, "
                    "internal_consistency, novelty, verification_outcome, usefulness, "
                    "verification_context, rated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        kr_entity,
                        kr_rater,
                        c_hash,
                        _clamp_score(kr.get("specificity", 0.0)),
                        _clamp_score(kr.get("falsifiability", 0.0)),
                        _clamp_score(kr.get("internal_consistency", 0.0)),
                        _clamp_score(kr.get("novelty", 0.0)),
                        kr.get("verification_outcome"),
                        kr.get("usefulness"),
                        kr.get("verification_context"),
                        kr.get("rated_at", now),
                    ),
                )
                imported_ratings += 1
            except (sqlite3.IntegrityError, sqlite3.OperationalError):
                continue
        if imported_ratings:
            logger.info("bridge_pull: imported %d knowledge ratings", imported_ratings)

    if staged_count:
        logger.info("bridge_pull: staged %d shared tasks for review", staged_count)
    if staged_knowledge:
        logger.info(
            "bridge_pull: staged %d shared entities, %d relations for knowledge review",
            staged_knowledge,
            staged_relations,
        )

    logger.info(
        "bridge_pull: %d new entities, %d new observations, %d new relations, "
        "%d new tasks, %d updated tasks, %d staged for review",
        new_entities,
        new_observations,
        new_relations,
        new_tasks,
        updated_tasks,
        staged_count,
    )
    result: dict[str, Any] = {
        "new_entities": new_entities,
        "new_observations": new_observations,
        "new_relations": new_relations,
        "new_tasks": new_tasks,
        "updated_tasks": updated_tasks,
        "source_machine": payload.get("machine_id", "unknown"),
        "pushed_at": payload.get("pushed_at", "unknown"),
    }
    if git_pull_failed:
        result["git_pull_failed"] = True
    if staged_count:
        result["staged_shared_tasks"] = staged_count
        result["review_required"] = (
            f"{staged_count} shared task(s) pending review. "
            "Use review_shared_tasks() to approve or reject."
        )
    if staged_knowledge:
        result["staged_shared_knowledge"] = staged_knowledge
        result["staged_shared_relations"] = staged_relations
        msg = f"{staged_knowledge} shared entit(ies) pending review"
        if staged_relations:
            msg += f" + {staged_relations} relation(s)"
        msg += ". Use review_shared_knowledge() to approve or reject."
        result["knowledge_review_required"] = msg
    if staged_public:
        result["staged_public_knowledge"] = staged_public
    if imported_ratings:
        result["imported_ratings"] = imported_ratings
    return json.dumps(result)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 3: bridge_status
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def bridge_status() -> str:
    """Show bridge sync status — local shared entities vs repo contents."""
    if not Path(BRIDGE_REPO).is_dir():
        return _error(f"Bridge repo not found at {BRIDGE_REPO}")

    with _get_conn() as conn:
        local_rows = conn.execute(
            "SELECT name FROM entities WHERE project LIKE 'shared%' ORDER BY name"
        ).fetchall()
        local_task_count = TaskDAO.count_active(conn)

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

    shared_path = Path(BRIDGE_REPO) / "shared.json"
    remote_names: set[str] = set()
    remote_task_count = 0
    repo_meta = {}
    if shared_path.exists():
        try:
            payload = _json_loads(shared_path.read_text(encoding="utf-8"))
            remote_names = {e["name"] for e in payload.get("entities", [])}
            remote_task_count = len(payload.get("tasks", []))
            repo_meta = {
                "pushed_at": payload.get("pushed_at"),
                "machine_id": payload.get("machine_id"),
                "version": payload.get("version"),
                "owner": payload.get("owner"),
            }
        except (json.JSONDecodeError, OSError):
            pass

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
            "remote_tasks": remote_task_count,
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
# Tool 4: assign_task
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def assign_task(task_id: str, assignee: str | None = None) -> str:
    """Assign a task or note to a GitHub user for collaboration.

    Sets assignee field. On next bridge_push, the item will be
    pushed to https://github.com/{assignee}/memory-bridge.
    Pass assignee=None to unassign.
    """
    now = _now()
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM tasks WHERE id = ?", (task_id,)
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

        conn.execute(
            "UPDATE tasks SET assignee = ?, shared_by = ?, updated_at = ? WHERE id = ?",
            (assignee, shared_by, now, task_id),
        )
        _upsert_field_versions(conn, task_id, ("assignee", "shared_by"), now)

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
                    if (
                        _is_valid_timestamp(remote_ts)
                        and remote_ts > existing["updated_at"]
                    ):
                        conn.execute(
                            "UPDATE tasks SET title=?, description=?, status=?, priority=?, "
                            "section=?, due_date=?, project=?, parent_id=?, notes=?, "
                            "recurring=?, type=?, assignee=?, shared_by=?, updated_at=? "
                            "WHERE id=?",
                            (
                                t["title"],
                                t.get("description"),
                                t["status"],
                                t["priority"],
                                t["section"],
                                t.get("due_date"),
                                t.get("project"),
                                t.get("parent_id"),
                                t.get("notes"),
                                t.get("recurring"),
                                t.get("type", "task"),
                                t.get("assignee"),
                                t.get("shared_by"),
                                t["updated_at"],
                                tid,
                            ),
                        )
                        _upsert_field_versions(
                            conn, tid, _MERGEABLE_FIELDS, t.get("updated_at", _now())
                        )
                        imported += 1
                else:
                    TaskDAO.create(
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
                    )
                    _upsert_field_versions(
                        conn, tid, _MERGEABLE_FIELDS, t.get("updated_at", _now())
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
if __name__ == "__main__":
    mcp.run(transport="stdio")
