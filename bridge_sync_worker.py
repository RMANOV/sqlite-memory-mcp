"""Bridge sync worker — standalone module for memory bridge sync.

Exports full memory (entities + relations + tasks + public knowledge) to
shared.json + per-task files + index.json, then git push.
Called from task_tray.py's Sync button.

No FastMCP / server.py dependency — only db_utils for DB access.
"""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from db_utils import (
    BRIDGE_REPO,
    DB_PATH,
    PUBLISH_STANDBY_MINUTES,
    TASK_EXPORT_COLS,
    now_iso,
    sanitize_task_enums,
    # v2.0.0: Bridge Sync v2 — per-field LWW
    json_loads as _json_loads,
    json_dumps as _json_dumps,  # I5: canonical JSON serialiser from db_utils
    export_task_files,
    export_index_json,
    merge_import_tasks,
    migrate_to_per_task_files,
    get_conn,  # I3: managed connection — handles PRAGMAs, BEGIN/COMMIT/ROLLBACK, close
    fts_sync_entity,
)

log = logging.getLogger("bridge_sync_worker")

# Suppress console windows on Windows
_NOWIN: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)


# ── Helpers ──────────────────────────────────────────────────────────────


_SAFETY_THRESHOLD = 10  # Block sync if this many descriptions would be removed


def _progress(cb: Callable[[int, str], None] | None, pct: int, label: str) -> None:
    if cb is not None:
        cb(pct, label)


def _git(*args: str, bridge_dir: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=bridge_dir,
        capture_output=True,
        text=True,
        timeout=30,
        **_NOWIN,
    )


def _check_sync_safety(
    conn: sqlite3.Connection,
    bridge_dir: str,
    threshold: int = _SAFETY_THRESHOLD,
) -> dict:
    """Compare local DB state vs bridge files. Flag destructive content changes."""
    tasks_dir = Path(bridge_dir) / "tasks"
    if not tasks_dir.exists():
        return {"is_safe": True, "descriptions_removed": 0, "notes_removed": 0}

    stats = {
        "descriptions_added": 0,
        "descriptions_removed": 0,
        "notes_added": 0,
        "notes_removed": 0,
        "tasks_removed": 0,
    }

    for task_file in tasks_dir.glob("*.json"):
        try:
            bridge_task = _json_loads(task_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue

        tid = bridge_task.get("id")
        if not tid:
            continue

        local = conn.execute(
            "SELECT description, notes FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

        if not local:
            stats["tasks_removed"] += 1
            continue

        bridge_desc = bridge_task.get("description")
        local_desc = local["description"]
        if bridge_desc and not local_desc:
            stats["descriptions_removed"] += 1
        elif not bridge_desc and local_desc:
            stats["descriptions_added"] += 1

        bridge_notes = bridge_task.get("notes")
        local_notes = local["notes"]
        if bridge_notes and not local_notes:
            stats["notes_removed"] += 1
        elif not bridge_notes and local_notes:
            stats["notes_added"] += 1

    stats["is_safe"] = stats["descriptions_removed"] < threshold
    return stats


# ── Import / Export helpers ──────────────────────────────────────────────


def _import_remote_entities(conn: sqlite3.Connection, entities: list) -> int:
    """Import entities from remote shared.json that don't exist locally."""
    imported = 0
    for e in entities:
        existing = conn.execute(
            "SELECT id FROM entities WHERE name = ?", (e["name"],)
        ).fetchone()
        if existing:
            continue
        now = now_iso()
        eid = conn.execute(
            "INSERT INTO entities (name, entity_type, project, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (e["name"], e["entityType"], e.get("project") or "shared:bridge", now, now),
        ).lastrowid
        for o in e.get("observations", []):
            conn.execute(
                "INSERT INTO observations (entity_id, content, created_at) "
                "VALUES (?, ?, ?)",
                (eid, o["content"], o.get("createdAt", now)),
            )
        fts_sync_entity(conn, eid)
        imported += 1
    return imported


def _export_entities(conn: sqlite3.Connection) -> tuple[list, set]:
    """Export shared entities + observations. Returns (entities_list, entity_ids)."""
    rows = conn.execute(
        "SELECT id, name, entity_type, project, created_at, updated_at "
        "FROM entities WHERE project LIKE 'shared%' ORDER BY name"
    ).fetchall()
    entities, ids = [], set()
    for e in rows:
        ids.add(e["id"])
        obs = conn.execute(
            "SELECT content, created_at FROM observations "
            "WHERE entity_id = ? ORDER BY id",
            (e["id"],),
        ).fetchall()
        entities.append(
            {
                "name": e["name"],
                "entityType": e["entity_type"],
                "project": e["project"],
                "observations": [
                    {"content": o["content"], "createdAt": o["created_at"]} for o in obs
                ],
                "createdAt": e["created_at"],
                "updatedAt": e["updated_at"],
            }
        )
    return entities, ids


def _export_relations(conn: sqlite3.Connection, entity_ids: set) -> list:
    """Export relations between shared entities."""
    if not entity_ids:
        return []
    ph = ",".join("?" * len(entity_ids))
    ids = list(entity_ids)
    rows = conn.execute(
        f"SELECT ef.name AS from_name, et.name AS to_name, "
        f"r.relation_type, r.created_at FROM relations r "
        f"JOIN entities ef ON r.from_id = ef.id "
        f"JOIN entities et ON r.to_id = et.id "
        f"WHERE r.from_id IN ({ph}) AND r.to_id IN ({ph})",
        ids + ids,
    ).fetchall()
    return [
        {
            "from": r["from_name"],
            "to": r["to_name"],
            "relationType": r["relation_type"],
            "createdAt": r["created_at"],
        }
        for r in rows
    ]


def _export_tasks(conn: sqlite3.Connection) -> list[dict]:
    """Export all non-archived tasks."""
    rows = conn.execute(
        f"SELECT {TASK_EXPORT_COLS} "
        "FROM tasks WHERE status NOT IN ('archived', 'cancelled') ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def _export_public_knowledge(conn: sqlite3.Connection) -> tuple[list, list]:
    """Export public entities + public tasks."""
    ent_rows = conn.execute(
        "SELECT id, name, entity_type, project, created_at, updated_at "
        "FROM entities WHERE visibility='public' ORDER BY name"
    ).fetchall()
    pub_entities = []
    for pe in ent_rows:
        obs = conn.execute(
            "SELECT content, created_at FROM observations "
            "WHERE entity_id = ? ORDER BY id",
            (pe["id"],),
        ).fetchall()
        pub_entities.append(
            {
                "name": pe["name"],
                "entityType": pe["entity_type"],
                "project": pe["project"],
                "observations": [
                    {"content": o["content"], "createdAt": o["created_at"]} for o in obs
                ],
                "createdAt": pe["created_at"],
                "updatedAt": pe["updated_at"],
            }
        )
    task_rows = conn.execute(
        "SELECT id, title, description, status, priority, section, "
        "due_date, project, created_at, updated_at "
        "FROM tasks WHERE visibility='public' ORDER BY created_at"
    ).fetchall()
    pub_tasks = [dict(r) for r in task_rows]
    return pub_entities, pub_tasks


def _export_knowledge_ratings(conn: sqlite3.Connection) -> list:
    """Export knowledge ratings (graceful if table doesn't exist)."""
    try:
        rows = conn.execute(
            "SELECT entity_name, rater_id, content_hash, specificity, "
            "falsifiability, internal_consistency, novelty, "
            "verification_outcome, usefulness, verification_context, "
            "rated_at FROM knowledge_ratings ORDER BY rated_at"
        ).fetchall()
        return [dict(r) for r in rows] if rows else []
    except Exception:
        return []


def _merge_remote_tasks(tasks_out: list[dict], existing_data: dict) -> list[dict]:
    """Merge remote tasks: keep missing-locally, newer-wins update.

    DEPRECATED: Legacy title-based matching for shared.json backward compat only.
    Will be removed once all machines run Bridge v2 (per-task files + index.json).
    New code should use merge_import_tasks() from db_utils (UUID-based LWW).
    """
    remote_tasks = existing_data.get("tasks", [])
    local_titles = {t["title"] for t in tasks_out}

    # Keep remote tasks missing locally
    for rt in remote_tasks:
        if rt.get("title") and rt["title"] not in local_titles:
            tasks_out.append(rt)
            local_titles.add(rt["title"])

    # Update existing tasks where remote has newer updated_at
    local_by_title = {t["title"]: t for t in tasks_out}
    for rt in remote_tasks:
        title = rt.get("title")
        if not title or title not in local_by_title:
            continue
        lt = local_by_title[title]
        r_upd = rt.get("updated_at", "")
        l_upd = lt.get("updated_at", "")
        if r_upd > l_upd:
            sanitize_task_enums(rt)
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

    return tasks_out


# ── Main entry point ────────────────────────────────────────────────────


def main(
    progress_callback: Callable[[int, str], None] | None = None,
    db_path: str | None = None,
    bridge_repo: str | None = None,
    force: bool = False,
) -> dict:
    """Run full bridge sync: pull → LWW merge → export → push.

    Returns {"entities": N, "tasks": N, "pushed": bool}.
    When force=False (default), blocks push if too many descriptions would be lost.
    """
    bridge_dir = bridge_repo or BRIDGE_REPO
    _db_path = db_path or DB_PATH

    # Phase 1: Promote pending_public (short transaction)
    with get_conn(_db_path) as conn:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=PUBLISH_STANDBY_MINUTES)
        ).isoformat()
        conn.execute(
            "UPDATE entities SET visibility='public' "
            "WHERE visibility='pending_public' AND publish_requested_at <= ?",
            (cutoff,),
        )
        conn.execute(
            "UPDATE tasks SET visibility='public' "
            "WHERE visibility='pending_public' AND publish_requested_at <= ?",
            (cutoff,),
        )

    # Phase 2: Git pull (no transaction held)
    _progress(progress_callback, 5, "git pull...")
    pull_result = _git("pull", "--rebase", "--autostash", bridge_dir=bridge_dir)
    if pull_result.returncode != 0:
        log.error("git pull failed: %s", pull_result.stderr)
        _progress(progress_callback, 100, "Done (pull failed)")
        return {
            "entities": 0,
            "tasks": 0,
            "pushed": False,
            "imported_new": 0,
            "imported_updated": 0,
        }

    # Phase 3a: Import (write transaction)
    with get_conn(_db_path) as conn:
        migrate_to_per_task_files(bridge_dir)

        _progress(progress_callback, 10, "Importing remote data...")
        shared_path = Path(bridge_dir) / "shared.json"
        index_path = Path(bridge_dir) / "index.json"
        new_t, upd_t = 0, 0

        if shared_path.exists():
            try:
                remote_data = _json_loads(shared_path.read_text(encoding="utf-8"))
                try:
                    _import_remote_entities(conn, remote_data.get("entities", []))
                except Exception as exc:
                    log.warning("Entity import failed: %s", exc)
            except (json.JSONDecodeError, OSError):
                pass

        if index_path.exists():
            try:
                idx_data = _json_loads(index_path.read_text(encoding="utf-8"))
                new_t, upd_t = merge_import_tasks(conn, idx_data.get("tasks", []))
                log.info("LWW merged %d new tasks, %d field updates", new_t, upd_t)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("index.json merge failed: %s", exc)
        elif shared_path.exists():
            try:
                remote_data = _json_loads(shared_path.read_text(encoding="utf-8"))
                new_t, upd_t = merge_import_tasks(conn, remote_data.get("tasks", []))
                log.info("Imported %d new, updated %d from remote", new_t, upd_t)
            except (json.JSONDecodeError, OSError):
                pass
    # Import transaction closed — DB lock released

    # Safety valve: check BEFORE export (bridge files still contain remote data)
    if not force:
        with get_conn(_db_path) as conn:
            safety = _check_sync_safety(conn, bridge_dir)
        if not safety["is_safe"]:
            log.warning(
                "SAFETY VALVE: %d descriptions would be removed, %d notes would be removed",
                safety["descriptions_removed"],
                safety["notes_removed"],
            )
            _progress(
                progress_callback,
                -1,
                f"BLOCKED: {safety['descriptions_removed']} descriptions + "
                f"{safety['notes_removed']} notes would be deleted. "
                f"Run with --force to override.",
            )
            return {
                "entities": 0,
                "tasks": 0,
                "pushed": False,
                "imported_new": new_t,
                "imported_updated": upd_t,
                "blocked_by_safety": True,
                "safety": safety,
            }

    # Phase 3b: Export (read-only, separate short transaction)
    with get_conn(_db_path) as conn:
        _progress(progress_callback, 20, "Exporting entities...")
        entities_out, entity_ids = _export_entities(conn)
        _progress(progress_callback, 30, "Exporting relations...")
        relations_out = _export_relations(conn, entity_ids)
        _progress(progress_callback, 40, "Exporting tasks...")
        tasks_out = _export_tasks(conn)

        _progress(progress_callback, 45, "Exporting per-task files...")
        export_task_files(conn, bridge_dir)
        export_index_json(conn, bridge_dir)

        _progress(progress_callback, 50, "Exporting public knowledge...")
        pub_entities, pub_tasks = _export_public_knowledge(conn)
        _progress(progress_callback, 55, "Exporting knowledge ratings...")
        kr_out = _export_knowledge_ratings(conn)
    # Export transaction closed

    # Phase 4: Build payload + write files + git ops (no transaction)
    _progress(progress_callback, 60, "Merging tasks...")
    payload = {
        "version": 3,
        "pushed_at": now_iso(),
        "machine_id": socket.gethostname(),
        "entities": entities_out,
        "relations": relations_out,
        "tasks": tasks_out,
    }
    if pub_entities or pub_tasks:
        payload["public_knowledge"] = {
            "entities": pub_entities,
            "tasks": pub_tasks,
        }
    if kr_out:
        payload["knowledge_ratings"] = kr_out

    # Gen-B guard: only run legacy merge when index.json doesn't exist
    if shared_path.exists():
        try:
            existing = _json_loads(shared_path.read_text(encoding="utf-8"))
            if not index_path.exists():
                _merge_remote_tasks(tasks_out, existing)

            if "ui_profiles" in existing:
                payload["ui_profiles"] = existing["ui_profiles"]
        except (json.JSONDecodeError, OSError):
            pass

    _progress(progress_callback, 70, "Writing shared.json...")
    shared_path.write_text(_json_dumps(payload), encoding="utf-8")

    _progress(progress_callback, 80, "git add...")
    _git("add", "shared.json", "index.json", "tasks/", bridge_dir=bridge_dir)

    _progress(progress_callback, 90, "git commit...")
    n_ent = len(entities_out)
    n_tasks = len(payload["tasks"])
    msg = f"bridge: push {n_ent} entities, {n_tasks} tasks from {socket.gethostname()}"
    result = _git("commit", "-m", msg, bridge_dir=bridge_dir)

    if result.returncode != 0:
        if "nothing to commit" not in (result.stdout + result.stderr):
            log.error("bridge sync commit failed: %s", result.stderr)
            _progress(progress_callback, 100, "Done")
            return {
                "entities": n_ent,
                "tasks": n_tasks,
                "pushed": False,
                "imported_new": new_t,
                "imported_updated": upd_t,
            }

    _progress(progress_callback, 95, "git push...")
    push_result = _git("push", bridge_dir=bridge_dir)
    pushed = push_result.returncode == 0

    # Deploy to Cloudflare Pages (auto-update after push)
    deployed = False
    if pushed:
        _progress(progress_callback, 97, "CF Pages deploy...")
        try:
            deploy_result = subprocess.run(
                ["wrangler", "pages", "deploy", bridge_dir,
                 "--project-name=memory-bridge", "--branch=main"],
                capture_output=True, text=True, timeout=60, **_NOWIN,
            )
            deployed = deploy_result.returncode == 0
            if not deployed:
                log.warning("CF Pages deploy failed: %s", deploy_result.stderr)
        except FileNotFoundError:
            log.warning("wrangler not found — skipping CF Pages deploy")
        except subprocess.TimeoutExpired:
            log.warning("CF Pages deploy timed out")

    _progress(progress_callback, 100, "Done")
    return {
        "entities": n_ent,
        "tasks": n_tasks,
        "pushed": pushed,
        "deployed": deployed,
        "imported_new": new_t,
        "imported_updated": upd_t,
    }
