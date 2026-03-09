"""Bridge sync worker — standalone module for memory bridge sync.

Exports full memory (entities + relations + tasks + public knowledge) to
shared.json, then git push.  Called from task_tray.py's Sync button.

No FastMCP / server.py dependency — only db_utils for DB access.
"""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from db_utils import (
    BRIDGE_REPO,
    DB_PATH,
    PUBLISH_STANDBY_MINUTES,
    now_iso,
    sanitize_task_enums,
)

log = logging.getLogger("bridge_sync_worker")

# Suppress console windows on Windows
_NOWIN: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)


# ── Helpers ──────────────────────────────────────────────────────────────


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
        imported += 1
    return imported


def _import_remote_tasks(conn: sqlite3.Connection, remote_tasks: list) -> tuple[int, int]:
    """Import/update local tasks from remote shared.json. Returns (new, updated)."""
    now = now_iso()
    new_count = 0
    updated_count = 0

    # Sort parents before children to avoid FK violations
    tasks_sorted = sorted(
        remote_tasks,
        key=lambda t: (t.get("parent_id") is not None, t.get("created_at", "")),
    )

    for task in tasks_sorted:
        sanitize_task_enums(task)
        tid = task.get("id")
        title = task.get("title")
        if not title:
            continue

        # Match by id first, then by title as fallback
        existing = None
        if tid:
            existing = conn.execute(
                "SELECT id, updated_at FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        if not existing:
            existing = conn.execute(
                "SELECT id, updated_at FROM tasks WHERE title = ?", (title,)
            ).fetchone()

        if existing:
            # Only overwrite if remote is newer
            if task.get("updated_at", "") > (existing["updated_at"] or ""):
                conn.execute(
                    "UPDATE tasks SET title=?, description=?, status=?, priority=?, "
                    "section=?, due_date=?, project=?, parent_id=?, notes=?, "
                    "recurring=?, type=?, assignee=?, shared_by=?, updated_at=? WHERE id=?",
                    (
                        title,
                        task.get("description"),
                        task.get("status", "not_started"),
                        task.get("priority", "medium"),
                        task.get("section", "inbox"),
                        task.get("due_date"),
                        task.get("project"),
                        task.get("parent_id"),
                        task.get("notes"),
                        task.get("recurring"),
                        task.get("type", "task"),
                        task.get("assignee"),
                        task.get("shared_by"),
                        task["updated_at"],
                        existing["id"],
                    ),
                )
                updated_count += 1
        else:
            # Insert new task — generate UUID if missing
            if not tid:
                tid = str(uuid.uuid4())
            conn.execute(
                "INSERT OR IGNORE INTO tasks (id, title, description, status, priority, "
                "section, due_date, project, parent_id, notes, recurring, "
                "type, assignee, shared_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tid,
                    title,
                    task.get("description"),
                    task.get("status", "not_started"),
                    task.get("priority", "medium"),
                    task.get("section", "inbox"),
                    task.get("due_date"),
                    task.get("project"),
                    task.get("parent_id"),
                    task.get("notes"),
                    task.get("recurring"),
                    task.get("type", "task"),
                    task.get("assignee"),
                    task.get("shared_by"),
                    task.get("created_at", now),
                    task.get("updated_at", now),
                ),
            )
            new_count += 1

    return new_count, updated_count


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
                    {"content": o["content"], "createdAt": o["created_at"]}
                    for o in obs
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
        "SELECT id, title, description, status, priority, section, "
        "due_date, project, parent_id, notes, recurring, type, "
        "assignee, shared_by, created_at, updated_at "
        "FROM tasks WHERE status != 'archived' ORDER BY created_at"
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
                    {"content": o["content"], "createdAt": o["created_at"]}
                    for o in obs
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
    """Merge remote tasks: keep missing-locally, newer-wins update."""
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
                "status", "section", "priority", "due_date",
                "notes", "description", "type",
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
) -> dict:
    """Run full bridge sync: pull → export → merge → push.

    Returns {"entities": N, "tasks": N, "pushed": bool}.
    """
    bridge_dir = bridge_repo or BRIDGE_REPO
    _db_path = db_path or DB_PATH

    conn = sqlite3.connect(_db_path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        # Promote pending_public → public if standby elapsed
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=PUBLISH_STANDBY_MINUTES)
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
        conn.commit()

        # 1. Pull remote changes
        _progress(progress_callback, 5, "git pull...")
        _git("pull", "--rebase", bridge_dir=bridge_dir)

        # 2. Import remote data (entities + tasks)
        _progress(progress_callback, 10, "Importing remote data...")
        shared_path = Path(bridge_dir) / "shared.json"
        new_t, upd_t = 0, 0
        if shared_path.exists():
            try:
                remote_data = json.loads(shared_path.read_text(encoding="utf-8"))
                conn.execute("BEGIN")
                _import_remote_entities(conn, remote_data.get("entities", []))
                new_t, upd_t = _import_remote_tasks(conn, remote_data.get("tasks", []))
                conn.commit()
                log.info("Imported %d new tasks, updated %d from remote", new_t, upd_t)
            except (json.JSONDecodeError, OSError):
                pass

        # 3. Export entities + observations
        _progress(progress_callback, 20, "Exporting entities...")
        entities_out, entity_ids = _export_entities(conn)

        # 4. Export relations
        _progress(progress_callback, 30, "Exporting relations...")
        relations_out = _export_relations(conn, entity_ids)

        # 5. Export tasks
        _progress(progress_callback, 40, "Exporting tasks...")
        tasks_out = _export_tasks(conn)

        # 6. Export public knowledge
        _progress(progress_callback, 50, "Exporting public knowledge...")
        pub_entities, pub_tasks = _export_public_knowledge(conn)

        # 7. Export knowledge ratings
        _progress(progress_callback, 55, "Exporting knowledge ratings...")
        kr_out = _export_knowledge_ratings(conn)

        # 8. Build payload
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

        # Merge remote tasks + preserve extra keys from existing shared.json
        if shared_path.exists():
            try:
                existing = json.loads(shared_path.read_text(encoding="utf-8"))
                _merge_remote_tasks(tasks_out, existing)

                known_keys = {
                    "version", "pushed_at", "machine_id", "entities",
                    "relations", "tasks", "shared_tasks", "public_knowledge",
                    "knowledge_ratings", "owner", "team_manifest", "ui_profiles",
                }
                for k, v in existing.items():
                    if k not in known_keys and isinstance(v, (list, dict)):
                        payload[k] = v

                # Preserve ui_profiles (task_tray patches own profile after main())
                if "ui_profiles" in existing:
                    payload["ui_profiles"] = existing["ui_profiles"]
            except (json.JSONDecodeError, OSError):
                pass

        # 9. Write shared.json
        _progress(progress_callback, 70, "Writing shared.json...")
        shared_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # 10. Git add + commit + push
        _progress(progress_callback, 80, "git add...")
        _git("add", "shared.json", bridge_dir=bridge_dir)

        _progress(progress_callback, 90, "git commit...")
        n_ent = len(entities_out)
        n_tasks = len(payload["tasks"])
        msg = f"bridge: push {n_ent} entities, {n_tasks} tasks from {socket.gethostname()}"
        result = _git("commit", "-m", msg, bridge_dir=bridge_dir)
        pushed = result.returncode == 0

        if pushed:
            _progress(progress_callback, 95, "git push...")
            _git("push", bridge_dir=bridge_dir)

        _progress(progress_callback, 100, "Done")
        return {
            "entities": n_ent,
            "tasks": n_tasks,
            "pushed": pushed,
            "imported_new": new_t,
            "imported_updated": upd_t,
        }

    finally:
        conn.close()
