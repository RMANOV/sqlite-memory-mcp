"""Bridge sync worker — standalone module for memory bridge sync.

Exports full memory (entities + relations + tasks + public knowledge) to
shared.json + per-task files + index.json, then git push.
Called from task_tray.py's Sync button.

No FastMCP / server.py dependency — only db_utils for DB access.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from db_utils import (
    BRIDGE_REPO,
    DB_PATH,
    PUBLISH_STANDBY_MINUTES,
    TASK_EXPORT_COLS,
    git_run,
    git_retry,
    _NOWIN,
    serialize_entity,
    export_relations,
    now_iso,
    sanitize_task_enums,
    # v2.0.0: Bridge Sync v2 — per-field LWW
    json_loads as _json_loads,
    json_dumps as _json_dumps,  # I5: canonical JSON serialiser from db_utils
    export_task_files,
    export_index_json,
    load_task_content,
    CONTENT_FIELDS,
    content_length,
    has_meaningful_content,
    is_suspicious_content_shrink,
    merge_import_tasks,
    migrate_to_per_task_files,
    get_conn,  # I3: managed connection — handles PRAGMAs, BEGIN/COMMIT/ROLLBACK, close
    fts_sync_entity,
    export_entity_files,
    export_entities_index,
    load_entities_from_files,
    migrate_entities_to_per_files,
    ensure_bridge_repo_ready,
)

log = logging.getLogger("bridge_sync_worker")


# ── Helpers ──────────────────────────────────────────────────────────────


_SAFETY_THRESHOLD = 10  # Block sync if this many descriptions would be removed
_SYNC_THREAD_LOCK = threading.Lock()


class _RepoSyncLock:
    """Cross-process repo lock for bridge sync."""

    def __init__(self, bridge_dir: str):
        self._path = Path(bridge_dir) / ".bridge_sync.lock"
        self._fh = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = self._path.open("a+", encoding="utf-8")
        try:
            fh.seek(0)
            fh.write("0")
            fh.flush()
            fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False

        self._fh = fh
        return True

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()
            self._fh = None


def _progress(cb: Callable[[int, str], None] | None, pct: int, label: str) -> None:
    if cb is not None:
        cb(pct, label)


def _write_shared_js(shared_path: Path, payload_text: str) -> None:
    js_path = shared_path.with_name("shared.js")
    tmp_path = js_path.with_suffix(".tmp")
    tmp_path.write_text(f"window.__BRIDGE_DATA__ = {payload_text};", encoding="utf-8")
    os.replace(tmp_path, js_path)


def _ui_profile_changed(
    shared_path: Path, machine_id: str, ui_profile: dict | None
) -> bool:
    """Return True when a tray UI profile needs to be exported."""
    if ui_profile is None:
        return False
    if not shared_path.exists():
        return True
    try:
        existing = _json_loads(shared_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return True
    profiles = existing.get("ui_profiles", {})
    return profiles.get(machine_id) != ui_profile


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
        "descriptions_shrunk": 0,
        "notes_added": 0,
        "notes_removed": 0,
        "notes_shrunk": 0,
        "tasks_removed": 0,
        "examples": [],
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
        if has_meaningful_content(bridge_desc) and not has_meaningful_content(
            local_desc
        ):
            stats["descriptions_removed"] += 1
        elif not has_meaningful_content(bridge_desc) and has_meaningful_content(
            local_desc
        ):
            stats["descriptions_added"] += 1
        elif is_suspicious_content_shrink(bridge_desc, local_desc):
            stats["descriptions_shrunk"] += 1
            if len(stats["examples"]) < 5:
                stats["examples"].append(
                    {
                        "task_id": tid,
                        "field": "description",
                        "bridge_len": content_length(bridge_desc),
                        "local_len": content_length(local_desc),
                    }
                )

        bridge_notes = bridge_task.get("notes")
        local_notes = local["notes"]
        if has_meaningful_content(bridge_notes) and not has_meaningful_content(
            local_notes
        ):
            stats["notes_removed"] += 1
        elif not has_meaningful_content(bridge_notes) and has_meaningful_content(
            local_notes
        ):
            stats["notes_added"] += 1
        elif is_suspicious_content_shrink(bridge_notes, local_notes):
            stats["notes_shrunk"] += 1
            if len(stats["examples"]) < 5:
                stats["examples"].append(
                    {
                        "task_id": tid,
                        "field": "notes",
                        "bridge_len": content_length(bridge_notes),
                        "local_len": content_length(local_notes),
                    }
                )

    stats["is_safe"] = (
        stats["descriptions_removed"] < threshold
        and stats["descriptions_shrunk"] == 0
        and stats["notes_shrunk"] == 0
    )
    return stats


# ── Import / Export helpers ──────────────────────────────────────────────


def _import_remote_entities(conn: sqlite3.Connection, entities: list) -> int:
    """Import entities from remote shared.json that don't exist locally.

    Per-entity error handling: one bad entity does not abort the rest.
    """
    imported = 0
    for e in entities:
        try:
            existing = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (e["name"],)
            ).fetchone()
            if existing:
                continue
            now = now_iso()
            eid = conn.execute(
                "INSERT INTO entities (name, entity_type, project, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    e["name"],
                    e["entityType"],
                    e.get("project") or "shared:bridge",
                    now,
                    now,
                ),
            ).lastrowid
            for o in e.get("observations", []):
                conn.execute(
                    "INSERT INTO observations (entity_id, content, created_at) "
                    "VALUES (?, ?, ?)",
                    (eid, o["content"], o.get("createdAt", now)),
                )
            fts_sync_entity(conn, eid)
            imported += 1
        except (sqlite3.OperationalError, sqlite3.IntegrityError, KeyError) as exc:
            log.warning("Entity import failed for %s: %s", e.get("name"), exc)
            continue
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
        entities.append(serialize_entity(conn, e, include_timestamps=True))
    return entities, ids


def _export_relations(conn: sqlite3.Connection, entity_ids: set) -> list:
    """Export relations between shared entities."""
    return export_relations(conn, entity_ids, include_timestamps=True)


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

    # Batch-load all observations for public entities (eliminates N+1 query)
    obs_map: dict[int, list] = {}
    if ent_rows:
        ent_ids = [pe["id"] for pe in ent_rows]
        placeholders = ",".join("?" * len(ent_ids))
        obs_rows = conn.execute(
            f"SELECT entity_id, content, created_at FROM observations "
            f"WHERE entity_id IN ({placeholders}) ORDER BY entity_id, id",
            ent_ids,
        ).fetchall()
        for o in obs_rows:
            obs_map.setdefault(o["entity_id"], []).append(
                {"content": o["content"], "createdAt": o["created_at"]}
            )

    pub_entities = []
    for pe in ent_rows:
        pub_entities.append(
            {
                "name": pe["name"],
                "entityType": pe["entity_type"],
                "project": pe["project"],
                "observations": obs_map.get(pe["id"], []),
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
    except sqlite3.OperationalError:
        return []


def _merge_remote_tasks(tasks_out: list[dict], existing_data: dict) -> list[dict]:
    """Merge remote tasks: keep missing-locally, newer-wins update.

    DEPRECATED: Legacy title-based matching for shared.json backward compat only.
    Will be removed once all machines run Bridge v2 (per-task files + index.json).
    New code should use merge_import_tasks() from db_utils (UUID-based LWW).
    """
    remote_tasks = existing_data.get("tasks", [])
    local_ids = {t["id"] for t in tasks_out}

    # Keep remote tasks missing locally
    for rt in remote_tasks:
        if rt.get("id") and rt["id"] not in local_ids:
            tasks_out.append(rt)
            local_ids.add(rt["id"])

    # Update existing tasks where remote has newer updated_at
    local_by_id = {t["id"]: t for t in tasks_out}
    for rt in remote_tasks:
        rt_id = rt.get("id")
        if not rt_id or rt_id not in local_by_id:
            continue
        lt = local_by_id[rt_id]
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


def _merge_remote_entities(entities_out: list, existing_data: dict) -> list:
    """Keep remote entities missing from local export.

    Mirrors _merge_remote_tasks — prevents overwriting remote-only entities
    when local export doesn't contain them (e.g. Win pushed entities that
    fedora hasn't imported yet).
    """
    remote_entities = existing_data.get("entities", [])
    local_names = {e["name"] for e in entities_out}
    for re in remote_entities:
        if re.get("name") and re["name"] not in local_names:
            entities_out.append(re)
            local_names.add(re["name"])
    return entities_out


# ── Main entry point ────────────────────────────────────────────────────


def main(
    progress_callback: Callable[[int, str], None] | None = None,
    db_path: str | None = None,
    bridge_repo: str | None = None,
    force: bool = False,
    ui_profile: dict | None = None,
) -> dict:
    """Run full bridge sync: pull → LWW merge → export → push.

    Returns {"entities": N, "tasks": N, "pushed": bool}.
    When force=False (default), blocks push if too many descriptions would be lost.
    """
    bridge_dir = bridge_repo or BRIDGE_REPO
    _db_path = db_path or DB_PATH
    machine_id = socket.gethostname()
    repo_lock = _RepoSyncLock(bridge_dir)

    if not _SYNC_THREAD_LOCK.acquire(blocking=False):
        return {
            "entities": 0,
            "tasks": 0,
            "pushed": False,
            "imported_new": 0,
            "imported_updated": 0,
            "already_running": True,
        }
    if not repo_lock.acquire():
        _SYNC_THREAD_LOCK.release()
        return {
            "entities": 0,
            "tasks": 0,
            "pushed": False,
            "imported_new": 0,
            "imported_updated": 0,
            "already_running": True,
        }

    try:
        return _main_locked(
            progress_callback=progress_callback,
            db_path=_db_path,
            bridge_dir=bridge_dir,
            force=force,
            ui_profile=ui_profile,
            machine_id=machine_id,
        )
    finally:
        repo_lock.release()
        _SYNC_THREAD_LOCK.release()


def _main_locked(
    progress_callback: Callable[[int, str], None] | None,
    db_path: str,
    bridge_dir: str,
    force: bool,
    ui_profile: dict | None,
    machine_id: str,
) -> dict:
    """Run sync with process/thread locks already held."""
    _db_path = db_path

    repo_ok, repo_msg = ensure_bridge_repo_ready(bridge_dir)
    if not repo_ok:
        log.warning("Bridge repo preflight blocked sync: %s", repo_msg)
        _progress(progress_callback, -1, f"BLOCKED: {repo_msg}")
        return {
            "entities": 0,
            "tasks": 0,
            "pushed": False,
            "imported_new": 0,
            "imported_updated": 0,
            "blocked_by_repo_state": True,
            "message": repo_msg,
        }

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
    pull_result = git_retry(bridge_dir, "pull", "--rebase", "--autostash")
    if pull_result.returncode != 0:
        log.error("git pull failed: %s", pull_result.stderr)
        # Log conflict for debugging
        _conflict_log = Path.home() / ".claude" / "memory" / "bridge_conflicts.log"
        try:
            with open(_conflict_log, "a", encoding="utf-8") as f:
                f.write(f"{now_iso()} git_pull_failed: {pull_result.stderr.strip()}\n")
        except OSError:
            pass
        # Auto-recover from merge conflicts: DB is source of truth, export will re-create
        _stderr = pull_result.stderr or ""
        if any(kw in _stderr for kw in ("unmerged", "conflict", "CONFLICT")):
            log.warning(
                "Merge conflict detected — aborting rebase and resetting to remote"
            )
            git_run(bridge_dir, "rebase", "--abort")
            git_run(bridge_dir, "reset", "--hard", "origin/main")
            log.warning("Reset to origin/main; export phase will re-create shared.json")
        else:
            _progress(progress_callback, 100, "Done (pull failed)")
            return {
                "entities": 0,
                "tasks": 0,
                "pushed": False,
                "imported_new": 0,
                "imported_updated": 0,
            }

    # Phase 3a-1: Import entities (own transaction — survives task merge failures)
    shared_path = Path(bridge_dir) / "shared.json"
    index_path = Path(bridge_dir) / "index.json"
    new_t, upd_t = 0, 0

    migrate_to_per_task_files(bridge_dir)
    migrate_entities_to_per_files(bridge_dir)

    with get_conn(_db_path) as conn:
        _progress(progress_callback, 10, "Importing remote entities...")
        remote_entities = load_entities_from_files(bridge_dir)
        if remote_entities:
            n_ent_imported = _import_remote_entities(conn, remote_entities)
            if n_ent_imported:
                log.info(
                    "Imported %d remote entities (from per-entity files)",
                    n_ent_imported,
                )
        elif shared_path.exists():
            try:
                remote_data = _json_loads(shared_path.read_text(encoding="utf-8"))
                n_ent_imported = _import_remote_entities(
                    conn, remote_data.get("entities", [])
                )
                if n_ent_imported:
                    log.info("Imported %d remote entities", n_ent_imported)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("shared.json read failed for entity import: %s", exc)
    # Entity import transaction closed — entities committed independently

    # Phase 3a-2: Import tasks (own transaction — entity imports safe even if this fails)
    with get_conn(_db_path) as conn:
        _progress(progress_callback, 15, "Importing remote tasks...")
        if index_path.exists():
            try:
                idx_data = _json_loads(index_path.read_text(encoding="utf-8"))
                remote_tasks = idx_data.get("tasks", [])

                # Enrich with content from per-task files (fixes dead load_task_content)
                enriched = 0
                for task in remote_tasks:
                    if task.get("_tombstone"):
                        continue
                    content = load_task_content(task.get("id", ""), bridge_dir)
                    if content:
                        for cf in CONTENT_FIELDS:
                            if cf in content:
                                task[cf] = content[cf]
                        if content.get("description") or content.get("notes"):
                            enriched += 1
                if enriched:
                    log.info(
                        "Enriched %d tasks with content from per-task files", enriched
                    )

                new_t, upd_t = merge_import_tasks(
                    conn, remote_tasks, import_content=True
                )
                log.info("LWW merged %d new tasks, %d field updates", new_t, upd_t)
            except (
                sqlite3.OperationalError,
                sqlite3.IntegrityError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                log.warning("index.json merge failed: %s", exc)
        elif shared_path.exists():
            try:
                remote_data = _json_loads(shared_path.read_text(encoding="utf-8"))
                new_t, upd_t = merge_import_tasks(
                    conn, remote_data.get("tasks", []), import_content=True
                )
                log.info("Imported %d new, updated %d from remote", new_t, upd_t)
            except (
                sqlite3.OperationalError,
                sqlite3.IntegrityError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                log.warning("Task merge from shared.json failed: %s", exc)
    # Task import transaction closed — DB lock released

    # Safety valve: check BEFORE export (bridge files still contain remote data)
    if not force:
        with get_conn(_db_path) as conn:
            safety = _check_sync_safety(conn, bridge_dir)
        if not safety["is_safe"]:
            log.warning(
                "SAFETY VALVE: %d descriptions removed, %d notes removed, "
                "%d descriptions shrunk, %d notes shrunk",
                safety["descriptions_removed"],
                safety["notes_removed"],
                safety["descriptions_shrunk"],
                safety["notes_shrunk"],
            )
            # Write notification for hook to surface to user
            _notify_path = (
                Path.home() / ".claude" / "memory" / "bridge_notifications.log"
            )
            try:
                with open(_notify_path, "a", encoding="utf-8") as f:
                    f.write(
                        f"{now_iso()} WARN safety_valve_block: "
                        f"{safety['descriptions_removed']} descriptions, "
                        f"{safety['notes_removed']} notes deleted, "
                        f"{safety['descriptions_shrunk']} descriptions shrunk, "
                        f"{safety['notes_shrunk']} notes shrunk\n"
                    )
            except OSError:
                pass
            _progress(
                progress_callback,
                -1,
                f"BLOCKED: {safety['descriptions_removed']} descriptions removed, "
                f"{safety['notes_removed']} notes removed, "
                f"{safety['descriptions_shrunk']} descriptions shrunk, "
                f"{safety['notes_shrunk']} notes shrunk. "
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

    # Incremental check: skip export+push if nothing changed since last push
    if not force:
        try:
            with get_conn(_db_path) as conn:
                meta_row = conn.execute(
                    "SELECT value FROM bridge_meta WHERE key = 'last_push_at'"
                ).fetchone()
                if meta_row:
                    last_push_at = meta_row["value"]
                    chk = conn.execute(
                        "SELECT "
                        "  (SELECT COUNT(*) FROM tasks WHERE updated_at > ?) AS ct, "
                        "  (SELECT COUNT(*) FROM entities WHERE updated_at > ?) AS ce, "
                        "  (SELECT COUNT(*) FROM entities "
                        "   WHERE visibility = 'pending_public') AS pp",
                        (last_push_at, last_push_at),
                    ).fetchone()
                    ui_profile_pending = _ui_profile_changed(
                        shared_path, machine_id, ui_profile
                    )
                    if (
                        chk["ct"] == 0
                        and chk["ce"] == 0
                        and chk["pp"] == 0
                        and not ui_profile_pending
                    ):
                        log.info(
                            "No changes since %s — skipping export+push", last_push_at
                        )
                        _progress(progress_callback, 100, "No changes — skipped push")
                        return {
                            "entities": 0,
                            "tasks": 0,
                            "pushed": False,
                            "imported_new": new_t,
                            "imported_updated": upd_t,
                            "skipped": True,
                        }
        except (sqlite3.OperationalError, AttributeError) as e:
            log.warning("Sync skip-check error: %s", e)

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

        _progress(progress_callback, 25, "Exporting per-entity files...")
        _, entity_rows = export_entity_files(conn, bridge_dir)
        export_entities_index(conn, bridge_dir, rows=entity_rows)

        _progress(progress_callback, 50, "Exporting public knowledge...")
        pub_entities, pub_tasks = _export_public_knowledge(conn)
        _progress(progress_callback, 55, "Exporting knowledge ratings...")
        kr_out = _export_knowledge_ratings(conn)
    # Export transaction closed

    # Phase 4: Build payload + write files + git ops (no transaction)
    _progress(progress_callback, 60, "Merging tasks...")
    payload = {
        "version": 4,
        "pushed_at": now_iso(),
        "machine_id": machine_id,
        "entities": [],  # backward compat — per-entity files are authoritative
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

            # Preserve remote-only entities only when per-entity files are not active
            entities_index_exists = (Path(bridge_dir) / "entities_index.json").exists()
            if not entities_index_exists:
                _merge_remote_entities(entities_out, existing)

            if "ui_profiles" in existing:
                payload["ui_profiles"] = dict(existing["ui_profiles"])
        except (json.JSONDecodeError, OSError):
            pass
    if ui_profile is not None:
        profiles = payload.setdefault("ui_profiles", {})
        profiles[machine_id] = ui_profile

    _progress(progress_callback, 70, "Writing shared.json...")
    payload_json = _json_dumps(payload)
    tmp_shared_path = shared_path.with_suffix(".tmp")
    tmp_shared_path.write_text(payload_json, encoding="utf-8")
    os.replace(tmp_shared_path, shared_path)
    _write_shared_js(shared_path, payload_json)

    _progress(progress_callback, 80, "git add...")
    git_run(
        bridge_dir,
        "add",
        "shared.json",
        "shared.js",
        "index.json",
        "tasks/",
        "entities/",
        "entities_index.json",
    )

    _progress(progress_callback, 90, "git commit...")
    n_ent = len(entities_out)
    n_tasks = len(payload["tasks"])
    msg = f"bridge: push {n_ent} entities, {n_tasks} tasks from {machine_id}"
    result = git_run(bridge_dir, "commit", "-m", msg)

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
    push_result = git_retry(bridge_dir, "push")
    pushed = push_result.returncode == 0

    # Record last_push_at so incremental check can skip next time
    if pushed:
        with get_conn(_db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bridge_meta(key, value) "
                "VALUES('last_push_at', ?)",
                (now_iso(),),
            )

    # Deploy to Cloudflare Pages (auto-update after push)
    deployed = False
    if pushed:
        _progress(progress_callback, 97, "CF Pages deploy...")
        try:
            deploy_result = subprocess.run(
                [
                    "wrangler",
                    "pages",
                    "deploy",
                    bridge_dir,
                    "--project-name=memory-bridge",
                    "--branch=main",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                **_NOWIN,
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
