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
import time
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
    apply_task_mutation,
    canonicalize_exported_task_statuses,
    git_run,
    git_retry,
    _NOWIN,
    record_memory_conflict,
    serialize_entity,
    export_relations,
    now_iso,
    sanitize_task_enums,
    # v2.0.0: Bridge Sync v2 — per-field LWW
    json_loads as _json_loads,
    json_dumps as _json_dumps,  # I5: canonical JSON serialiser from db_utils
    export_task_files,
    export_index_json,
    mark_tombstones_pushed,
    load_remote_tasks_for_merge,
    content_length,
    has_meaningful_content,
    is_archived_duplicate_redirect_task,
    is_suspicious_content_shrink,
    merge_import_tasks,
    migrate_to_per_task_files,
    get_conn,  # I3: managed connection — handles PRAGMAs, BEGIN/COMMIT/ROLLBACK, close
    fts_sync_entity,
    export_entity_files,
    export_entities_index,
    export_context_chunks,
    export_context_annotations,
    export_context_questions,
    export_candidate_claims,
    export_claim_evidence,
    export_canonical_facts,
    export_provenance_links,
    export_knowledge_links,
    export_memory_events,
    export_memory_audit_issues,
    export_memory_artifacts,
    export_memory_conflicts,
    export_memory_audit_state,
    import_remote_bridge_data,
    write_extended_memory_files,  # noqa: F401
    write_kanban_payload,  # noqa: F401
    ensure_kanban_payload_parseable,
    EXTENDED_MEMORY_KEYS,  # noqa: F401
    migrate_entities_to_per_files,
    ensure_bridge_repo_ready,
    ensure_bridge_git_identity,
    bridge_change_summary,
    promote_pending_public_entities,
    sync_task_attachments_from_remote,
)
from surface_contract import BRIDGE_GIT_STAGE_PATHS

log = logging.getLogger("bridge_sync_worker")


# ── Helpers ──────────────────────────────────────────────────────────────


_SAFETY_THRESHOLD = 10  # Block sync if this many descriptions would be removed
_SYNC_THREAD_LOCK = threading.Lock()
_GIT_PULL_TIMEOUT = 120
_GIT_PUSH_TIMEOUT = 300
_GIT_COMMIT_TIMEOUT = 60

# Push failure exponential backoff (process-local): 60s → 120s → 240s → 480s → cap 600s
_PUSH_BACKOFF_LOCK = threading.Lock()
_push_failure_count = 0
_push_backoff_until = 0.0  # monotonic time
_PUSH_BACKOFF_BASE = 60
_PUSH_BACKOFF_MAX = 600


def _sync_bridge_repo_fast_forward(bridge_dir: str) -> tuple[bool, str | None]:
    """Fetch remote bridge state without starting conflict-prone rebases."""
    fetch = git_retry(
        bridge_dir,
        "fetch",
        "origin",
        "main",
        timeout=_GIT_PULL_TIMEOUT,
    )
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout).strip()
        return False, f"bridge git fetch failed: {detail or 'unknown git error'}"

    local = git_retry(bridge_dir, "rev-parse", "HEAD", timeout=30)
    remote = git_retry(bridge_dir, "rev-parse", "origin/main", timeout=30)
    base = git_retry(bridge_dir, "merge-base", "HEAD", "origin/main", timeout=30)
    if local.returncode != 0 or remote.returncode != 0 or base.returncode != 0:
        detail = " ".join(
            (cp.stderr or cp.stdout).strip()
            for cp in (local, remote, base)
            if cp.returncode != 0
        ).strip()
        return (
            False,
            f"bridge git graph inspection failed: {detail or 'unknown git error'}",
        )

    local_sha = local.stdout.strip()
    remote_sha = remote.stdout.strip()
    base_sha = base.stdout.strip()
    if local_sha == remote_sha or base_sha == remote_sha:
        return True, None
    if base_sha == local_sha:
        merge = git_retry(
            bridge_dir,
            "merge",
            "--ff-only",
            "origin/main",
            timeout=_GIT_PULL_TIMEOUT,
        )
        if merge.returncode != 0:
            detail = (merge.stderr or merge.stdout).strip()
            return (
                False,
                f"bridge git fast-forward failed: {detail or 'unknown git error'}",
            )
        return True, None

    return (
        False,
        "bridge repo local and origin/main diverged; explicit recovery required "
        f"(local={local_sha[:12]}, remote={remote_sha[:12]}, base={base_sha[:12]})",
    )


class _RepoSyncLock:
    """Cross-process repo lock for bridge sync."""

    def __init__(self, bridge_dir: str):
        repo_root = Path(bridge_dir).resolve()
        self._path = repo_root.parent / f".{repo_root.name}.sync.lock"
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


def _tmp_write_path(path: Path) -> Path:
    """Use a per-target temp name so parallel bridge writers never share one tmp file."""
    return path.with_name(f"{path.name}.tmp")


def _write_shared_js(shared_path: Path, payload_text: str) -> None:
    js_path = shared_path.with_name("shared.js")
    tmp_path = _tmp_write_path(js_path)
    tmp_path.write_text(f"window.__BRIDGE_DATA__ = {payload_text};", encoding="utf-8")
    os.replace(tmp_path, js_path)


def _git_detail(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or result.stdout or "").strip() or "unknown git error"


def _ensure_stage_dirs(bridge_dir: str) -> None:
    for rel_path in BRIDGE_GIT_STAGE_PATHS:
        if rel_path.endswith("/"):
            (Path(bridge_dir) / rel_path.rstrip("/")).mkdir(parents=True, exist_ok=True)


def _stage_generated_bridge_artifacts(bridge_dir: str) -> subprocess.CompletedProcess:
    """Stage generated bridge artifacts without force-adding ignored large files."""
    normal_paths = tuple(path for path in BRIDGE_GIT_STAGE_PATHS if path != "shared.js")
    add_result = git_run(bridge_dir, "add", *normal_paths)
    if add_result.returncode != 0:
        return add_result
    return git_run(bridge_dir, "add", "-f", "shared.js")


def _ui_profile_changed(
    shared_path: Path, machine_id: str, ui_profile: dict | None
) -> bool:
    """Return True when a tray UI profile needs to be exported."""

    def _normalize(profile: dict | None) -> dict | None:
        if not isinstance(profile, dict):
            return profile
        normalized = dict(profile)
        # Ignore volatile timestamps when deciding whether the tray profile changed.
        normalized.pop("updated_at", None)
        return normalized

    if ui_profile is None:
        return False
    if not shared_path.exists():
        return True
    try:
        existing = _json_loads(shared_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return True
    profiles = existing.get("ui_profiles", {})
    return _normalize(profiles.get(machine_id)) != _normalize(ui_profile)


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
            "SELECT title, status, description, notes FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

        if not local:
            stats["tasks_removed"] += 1
            continue

        if is_archived_duplicate_redirect_task(local):
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


def _auto_heal_sync_safety(conn: sqlite3.Connection, bridge_dir: str) -> dict:
    """Restore richer bridge content into local DB before export when safe to do so."""
    tasks_dir = Path(bridge_dir) / "tasks"
    stats = {
        "restored_descriptions": 0,
        "restored_notes": 0,
        "tasks_touched": 0,
        "examples": [],
    }
    if not tasks_dir.exists():
        return stats

    for task_file in tasks_dir.glob("*.json"):
        try:
            bridge_task = _json_loads(task_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue

        tid = bridge_task.get("id")
        if not tid:
            continue

        local = conn.execute(
            "SELECT title, status, project, description, notes, updated_at "
            "FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        if not local:
            continue

        if is_archived_duplicate_redirect_task(local):
            continue

        changes = {}
        local_updated_at = local["updated_at"]
        bridge_updated_at = bridge_task.get("updated_at")
        for field in ("description", "notes"):
            bridge_value = bridge_task.get(field)
            local_value = local[field]
            if not has_meaningful_content(bridge_value):
                continue
            if not has_meaningful_content(local_value):
                rationale = "bridge content restored over empty local field"
            elif is_suspicious_content_shrink(bridge_value, local_value):
                rationale = "bridge content restored after suspicious local shrink"
            else:
                continue

            changes[field] = bridge_value
            if field == "description":
                stats["restored_descriptions"] += 1
            else:
                stats["restored_notes"] += 1
            if len(stats["examples"]) < 5:
                stats["examples"].append(
                    {
                        "task_id": tid,
                        "field": field,
                        "bridge_len": content_length(bridge_value),
                        "local_len": content_length(local_value),
                    }
                )
            record_memory_conflict(
                conn,
                aggregate_kind="task",
                aggregate_id=tid,
                field_name=field,
                local_value=local_value,
                remote_value=bridge_value,
                local_updated_at=local_updated_at,
                remote_updated_at=bridge_updated_at,
                winner="bridge_restore",
                rationale=rationale,
            )

        if not changes:
            continue

        apply_task_mutation(
            conn,
            tid,
            changes,
            tool_name="bridge_sync_worker.safety_restore",
            actor_id="bridge_sync_worker",
            source_kind="bridge_task",
            source_ref=tid,
        )
        stats["tasks_touched"] += 1

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
    tasks = [dict(r) for r in rows]
    canonicalize_exported_task_statuses(conn, tasks)
    return tasks


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
    canonicalize_exported_task_statuses(conn, pub_tasks)
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


def _export_extended_memory(conn: sqlite3.Connection) -> dict[str, list]:
    """Export append-only/event/provenance memory artifacts for cross-device sync."""
    return {
        "context_chunks": export_context_chunks(conn),
        "context_annotations": export_context_annotations(conn),
        "context_questions": export_context_questions(conn),
        "candidate_claims": export_candidate_claims(conn),
        "claim_evidence": export_claim_evidence(conn),
        "canonical_facts": export_canonical_facts(conn),
        "provenance_links": export_provenance_links(conn),
        "knowledge_links": export_knowledge_links(conn),
        "memory_events": export_memory_events(conn),
        "memory_audit_issues": export_memory_audit_issues(conn),
        "memory_artifacts": export_memory_artifacts(conn),
        "memory_conflicts": export_memory_conflicts(conn),
        "memory_audit_state": export_memory_audit_state(conn),
    }


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
    pull_only: bool = False,
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

    # Respect push-failure backoff (skip push attempts while in cooldown)
    if not pull_only:
        with _PUSH_BACKOFF_LOCK:
            remaining = _push_backoff_until - time.monotonic()
        if remaining > 0:
            return {
                "entities": 0,
                "tasks": 0,
                "pushed": False,
                "imported_new": 0,
                "imported_updated": 0,
                "backoff_active": True,
                "backoff_remaining_s": int(remaining),
                "message": f"push backoff active ({int(remaining)}s remaining after {_push_failure_count} failures)",
            }

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
            pull_only=pull_only,
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
    pull_only: bool,
    ui_profile: dict | None,
    machine_id: str,
) -> dict:
    """Run sync with process/thread locks already held."""
    _db_path = db_path
    export_started_at = now_iso()

    if not pull_only:
        identity = ensure_bridge_git_identity(bridge_dir)
        if identity.get("changed"):
            log.info(
                "Bridge git identity set to %s <%s>",
                identity.get("user_name") or "",
                identity.get("user_email") or "",
            )

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
    if not pull_only:
        with get_conn(_db_path) as conn:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(minutes=PUBLISH_STANDBY_MINUTES)
            ).isoformat()
            promote_pending_public_entities(conn, cutoff, export_started_at)
            rows = conn.execute(
                "SELECT id FROM tasks WHERE visibility='pending_public' "
                "AND publish_requested_at <= ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                apply_task_mutation(
                    conn,
                    row["id"],
                    {"visibility": "public"},
                    timestamp=export_started_at,
                    tool_name="bridge_sync_worker.promote_pending_public",
                )

    # Phase 2: Git fetch/fast-forward (no transaction held).
    # Avoid pull --rebase: generated bridge exports conflict frequently and a
    # failed rebase leaves the repo blocked until manual recovery.
    _progress(progress_callback, 5, "git fetch/ff...")
    sync_ok, sync_msg = _sync_bridge_repo_fast_forward(bridge_dir)
    if not sync_ok:
        detail = sync_msg or "unknown git error"
        log.error("git sync failed: %s", detail)
        # Log conflict for debugging
        _conflict_log = Path.home() / ".claude" / "memory" / "bridge_conflicts.log"
        try:
            with open(_conflict_log, "a", encoding="utf-8") as f:
                f.write(f"{now_iso()} git_sync_failed: {detail}\n")
        except OSError:
            pass
        message = (
            "git sync failed; bridge sync blocked before import/export: "
            f"{detail or 'unknown git error'}"
        )
        _progress(progress_callback, -1, f"BLOCKED: {message}")
        return {
            "entities": 0,
            "tasks": 0,
            "pushed": False,
            "imported_new": 0,
            "imported_updated": 0,
            "blocked_by_repo_state": True,
            "git_pull_failed": True,
            "message": message,
        }

    # Phase 3a-1: Import entities (own transaction — survives task merge failures)
    shared_path = Path(bridge_dir) / "shared.json"
    index_path = Path(bridge_dir) / "index.json"
    new_t, upd_t = 0, 0

    migrate_to_per_task_files(bridge_dir)
    migrate_entities_to_per_files(bridge_dir)

    remote_payload: dict = {}
    if shared_path.exists():
        try:
            remote_payload = _json_loads(shared_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            log.warning("shared.json read failed for merge: %s", exc)

    with get_conn(_db_path) as conn:
        _progress(progress_callback, 10, "Importing remote entities...")
        br = import_remote_bridge_data(conn, bridge_dir, remote_payload, log)
        if br["entities"] or br["relations"]:
            log.info(
                "Imported %d remote entities and %d relations",
                br["entities"],
                br["relations"],
            )
        if br["ratings"]:
            log.info("Imported %d remote knowledge ratings", br["ratings"])
    with get_conn(_db_path) as conn:
        _progress(progress_callback, 15, "Importing remote tasks...")
        remote_tasks, _loaded_from_index = load_remote_tasks_for_merge(
            bridge_dir,
            remote_payload,
            log,
        )
        merge_failed = False
        if remote_tasks:
            try:
                new_t, upd_t = merge_import_tasks(
                    conn,
                    remote_tasks,
                    import_content=True,
                    remote_events=remote_payload.get("memory_events", []),
                )
                sync_task_attachments_from_remote(conn, remote_tasks, bridge_dir)
                log.info("LWW merged %d new tasks, %d field updates", new_t, upd_t)
            except (
                sqlite3.OperationalError,
                sqlite3.IntegrityError,
                ValueError,
            ) as exc:
                log.warning("Task merge failed: %s", exc)
                merge_failed = True
    # Task import transaction closed — DB lock released

    if merge_failed and not pull_only:
        log.error(
            "sync aborted: task merge failed — local DB has not absorbed "
            "remote tombstones; pushing stale state would resurrect "
            "deletions made on other peers"
        )
        _progress(
            progress_callback,
            -1,
            "BLOCKED: task merge failed (DB lock contention). "
            "Tombstones from other peers not absorbed; push aborted.",
        )
        return {
            "entities": 0,
            "tasks": 0,
            "pushed": False,
            "imported_new": new_t,
            "imported_updated": upd_t,
            "blocked_by_merge_failure": True,
        }

    if pull_only:
        # No export follows in pull-only mode, so a union-merge-corrupted
        # kanban_payload.json would otherwise linger until the next full sync.
        # Parse-or-regenerate from the transport payload; never blocks the pull.
        ensure_kanban_payload_parseable(bridge_dir, remote_payload, log)
        _progress(progress_callback, 100, "Pull complete")
        return {
            "entities": 0,
            "tasks": 0,
            "pushed": False,
            "pull_only": True,
            "imported_new": new_t,
            "imported_updated": upd_t,
        }

    audit_summary: dict | None = None
    try:
        from memory_audit import maybe_run_memory_audit

        with get_conn(_db_path) as conn:
            _progress(progress_callback, 18, "Auditing memory...")
            audit_summary = maybe_run_memory_audit(
                conn,
                runner_name="bridge_sync",
                cadence_minutes=60,
                repair=True,
                stale_sync_minutes=120,
                emit_event=False,
            )
    except Exception as exc:
        log.warning("Memory audit failed during bridge sync: %s", exc)

    # Safety valve: check BEFORE export (bridge files still contain remote data)
    if not force:
        with get_conn(_db_path) as conn:
            repairs = _auto_heal_sync_safety(conn, bridge_dir)
            safety = _check_sync_safety(conn, bridge_dir)
        if repairs["tasks_touched"]:
            log.info(
                "Safety auto-restore repaired %d tasks (%d descriptions, %d notes)",
                repairs["tasks_touched"],
                repairs["restored_descriptions"],
                repairs["restored_notes"],
            )
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
                    cutoff = (
                        datetime.now(timezone.utc)
                        - timedelta(minutes=PUBLISH_STANDBY_MINUTES)
                    ).isoformat()
                    change_summary = bridge_change_summary(conn, last_push_at, cutoff)
                    ui_profile_pending = _ui_profile_changed(
                        shared_path, machine_id, ui_profile
                    )
                    if not any(change_summary.values()) and not ui_profile_pending:
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
    extended_memory: dict[str, list] = {}
    with get_conn(_db_path) as conn:
        _progress(progress_callback, 20, "Exporting entities...")
        entities_out, entity_ids = _export_entities(conn)
        _progress(progress_callback, 30, "Exporting relations...")
        relations_out = _export_relations(conn, entity_ids)
        _progress(progress_callback, 40, "Exporting tasks...")
        tasks_out = _export_tasks(conn)

        _progress(progress_callback, 45, "Exporting per-task files...")
        # Full export here (no changed_since): the returned id list contains every
        # task written to the payload, including tombstones. We stamp the pushed
        # tombstones from this exact list AFTER a successful push (see below).
        exported_task_ids = export_task_files(conn, bridge_dir)
        export_index_json(conn, bridge_dir)

        _progress(progress_callback, 25, "Exporting per-entity files...")
        _, entity_rows = export_entity_files(conn, bridge_dir)
        export_entities_index(conn, bridge_dir, rows=entity_rows)

        _progress(progress_callback, 50, "Exporting public knowledge...")
        pub_entities, pub_tasks = _export_public_knowledge(conn)
        _progress(progress_callback, 55, "Exporting knowledge ratings...")
        kr_out = _export_knowledge_ratings(conn)
        _progress(progress_callback, 58, "Exporting memory ledger...")
        extended_memory = _export_extended_memory(conn)
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
    # v5: write extended memory to separate files (keeps shared.json under CF Pages 25 MB limit)
    write_extended_memory_files(bridge_dir, extended_memory)
    for key in EXTENDED_MEMORY_KEYS:
        payload[key] = []  # empty placeholders for backward compat
    if pub_entities or pub_tasks:
        payload["public_knowledge"] = {
            "entities": pub_entities,
            "tasks": pub_tasks,
        }
    if kr_out:
        payload["knowledge_ratings"] = kr_out
    if audit_summary is not None:
        payload["memory_health"] = {
            "audit_version": audit_summary.get("audit_version"),
            "open_issue_count": audit_summary.get("open_issue_count", 0),
            "resolved_issue_count": audit_summary.get("resolved_issue_count", 0),
            "repairs": audit_summary.get("repairs", {}),
        }

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
    tmp_shared_path = _tmp_write_path(shared_path)
    tmp_shared_path.write_text(payload_json, encoding="utf-8")
    os.replace(tmp_shared_path, shared_path)
    _write_shared_js(shared_path, payload_json)
    # Render-only Kanban payload (preview); failure here must NOT block transport/push.
    try:
        write_kanban_payload(bridge_dir, payload)
    except Exception as exc:  # noqa: BLE001 - render artifact is best-effort
        log.warning(
            "kanban_payload write failed (non-fatal, transport unaffected): %s", exc
        )

    n_ent = len(entities_out)
    n_tasks = len(payload["tasks"])

    _progress(progress_callback, 80, "git add...")
    try:
        _ensure_stage_dirs(bridge_dir)
    except OSError as exc:
        message = f"git add failed before staging generated bridge artifacts: {exc}"
        log.error("bridge sync %s", message)
        _progress(progress_callback, -1, f"BLOCKED: {message}")
        return {
            "entities": n_ent,
            "tasks": n_tasks,
            "pushed": False,
            "imported_new": new_t,
            "imported_updated": upd_t,
            "git_add_failed": True,
            "message": message,
        }
    add_result = _stage_generated_bridge_artifacts(bridge_dir)
    if add_result.returncode != 0:
        detail = _git_detail(add_result)
        message = f"git add failed: {detail}"
        log.error("bridge sync %s", message)
        _progress(progress_callback, -1, f"BLOCKED: {message}")
        return {
            "entities": n_ent,
            "tasks": n_tasks,
            "pushed": False,
            "imported_new": new_t,
            "imported_updated": upd_t,
            "git_add_failed": True,
            "message": message,
        }

    _progress(progress_callback, 90, "git commit...")
    msg = f"bridge: push {n_ent} entities, {n_tasks} tasks from {machine_id}"
    result = git_run(bridge_dir, "commit", "-m", msg, timeout=_GIT_COMMIT_TIMEOUT)

    if result.returncode != 0:
        if "nothing to commit" not in (result.stdout + result.stderr):
            detail = _git_detail(result)
            message = f"git commit failed: {detail}"
            log.error("bridge sync %s", message)
            _progress(progress_callback, -1, f"BLOCKED: {message}")
            return {
                "entities": n_ent,
                "tasks": n_tasks,
                "pushed": False,
                "imported_new": new_t,
                "imported_updated": upd_t,
                "git_commit_failed": True,
                "message": message,
            }

    _progress(progress_callback, 95, "git push...")
    push_result = git_retry(bridge_dir, "push", timeout=_GIT_PUSH_TIMEOUT)
    pushed = push_result.returncode == 0
    push_message = (push_result.stderr or push_result.stdout or "").strip()
    if not pushed and push_message:
        log.warning("git push failed: %s", push_message)

    # Update push-failure backoff state (process-local)
    global _push_failure_count, _push_backoff_until
    with _PUSH_BACKOFF_LOCK:
        if pushed:
            _push_failure_count = 0
            _push_backoff_until = 0.0
        else:
            _push_failure_count += 1
            delay = min(
                _PUSH_BACKOFF_BASE * (2 ** (_push_failure_count - 1)),
                _PUSH_BACKOFF_MAX,
            )
            _push_backoff_until = time.monotonic() + delay
            log.warning(
                "push backoff: %d consecutive failures, skipping push for %ds",
                _push_failure_count,
                delay,
            )

    # Record last_push_at so incremental check can skip next time, and stamp the
    # tombstones that were just pushed (push-aware retention). Stamping uses the
    # exact id list export_task_files returned for THIS push, so only tombstones
    # actually in the payload become Tier-2 hard-delete eligible.
    if pushed:
        with get_conn(_db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bridge_meta(key, value) "
                "VALUES('last_push_at', ?)",
                (payload["pushed_at"],),
            )
            mark_tombstones_pushed(conn, exported_task_ids, payload["pushed_at"])

    # Deploy to Cloudflare Pages (auto-update after push)
    deployed = False
    if pushed and os.environ.get("CLOUDFLARE_API_TOKEN"):
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
        "message": push_message if not pushed else None,
    }
