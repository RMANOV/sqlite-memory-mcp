"""Cross-account bridge publishing helpers.

The primary bridge repository is synchronized by :mod:`bridge_sync_worker`.
This module owns the optional side effects that publish assigned tasks and
explicitly shared knowledge to collaborator repositories.  Database reads are
completed before any network process starts, so slow git operations never hold
an SQLite transaction open.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

from db_utils import (
    _NOWIN,
    get_conn,
    now_iso,
    parse_iso_datetime_for_compare,
    source_hash,
    validate_github_username,
)

log = logging.getLogger("bridge_peer_sync")


def _read_payload(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Ignoring corrupt peer payload %s: %s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write_payload(path: Path, payload: dict) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _git_detail(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or result.stdout or "").strip() or "unknown git error"


def _clone_merge_push(
    target_user: str,
    payload_key: str,
    items: list[dict],
    merge: Callable[[list[dict], list[dict]], list[dict]],
    message: str,
) -> bool:
    """Clone one peer repo, merge one payload section, and push it.

    A no-op merge is successful: the peer already has the requested state.
    """
    if not items:
        return True
    validate_github_username(target_user)
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
            log.warning("Peer clone failed for %s: %s", target_user, _git_detail(clone))
            return False

        shared_path = Path(tmpdir) / "shared.json"
        payload = _read_payload(shared_path)
        current = payload.get(payload_key, [])
        if not isinstance(current, list):
            current = []
        merged = merge(current, items)
        if merged == current:
            return True
        payload[payload_key] = merged
        _atomic_write_payload(shared_path, payload)

        add = subprocess.run(
            ["git", "-C", tmpdir, "add", "shared.json"],
            capture_output=True,
            text=True,
            timeout=10,
            **_NOWIN,
        )
        if add.returncode != 0:
            log.warning("Peer git add failed for %s: %s", target_user, _git_detail(add))
            return False
        commit = subprocess.run(
            ["git", "-C", tmpdir, "commit", "-m", message],
            capture_output=True,
            text=True,
            timeout=10,
            **_NOWIN,
        )
        if commit.returncode != 0:
            log.warning(
                "Peer git commit failed for %s: %s",
                target_user,
                _git_detail(commit),
            )
            return False
        push = subprocess.run(
            ["git", "-C", tmpdir, "push"],
            capture_output=True,
            text=True,
            timeout=30,
            **_NOWIN,
        )
        if push.returncode != 0:
            log.warning(
                "Peer git push failed for %s: %s", target_user, _git_detail(push)
            )
            return False
        return True


def _merge_tasks(current: list[dict], incoming: list[dict]) -> list[dict]:
    by_id = {
        str(item["id"]): item
        for item in current
        if isinstance(item, dict) and item.get("id")
    }
    for task in incoming:
        task_id = str(task.get("id") or "")
        if not task_id:
            continue
        existing = by_id.get(task_id, {})
        if parse_iso_datetime_for_compare(task.get("updated_at")) >= (
            parse_iso_datetime_for_compare(existing.get("updated_at"))
        ):
            by_id[task_id] = task
    return [by_id[key] for key in sorted(by_id)]


def _merge_knowledge(current: list[dict], incoming: list[dict]) -> list[dict]:
    by_hash = {
        str(item["sourceHash"]): item
        for item in current
        if isinstance(item, dict) and item.get("sourceHash")
    }
    for item in incoming:
        content_hash = str(item.get("sourceHash") or "")
        if content_hash:
            by_hash[content_hash] = item
    return [by_hash[key] for key in sorted(by_hash)]


def _knowledge_targets(db_path: str) -> list[str]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT sr.target_user "
            "FROM sharing_rules sr "
            "JOIN collaborators c ON c.github_user = sr.target_user "
            "WHERE sr.target_user <> '*' "
            "ORDER BY sr.target_user"
        ).fetchall()
    return [str(row["target_user"]) for row in rows]


def _build_knowledge_payload(db_path: str, target_user: str) -> list[dict]:
    """Materialize one collaborator payload, closing SQLite before git I/O."""
    with get_conn(db_path) as conn:
        rules = conn.execute(
            "SELECT entity_name, share_type, priority FROM sharing_rules "
            "WHERE target_user IN (?, '*')",
            (target_user,),
        ).fetchall()
        if not rules:
            return []

        names: set[str] = set()
        priorities: dict[str, str] = {}
        include_relations = False
        for rule in rules:
            share_type = str(rule["share_type"])
            name = str(rule["entity_name"])
            if share_type in ("entity", "all"):
                if name == "*":
                    rows = conn.execute(
                        "SELECT name FROM entities WHERE project LIKE 'shared%'"
                    ).fetchall()
                    for row in rows:
                        entity_name = str(row["name"])
                        names.add(entity_name)
                        priorities[entity_name] = str(rule["priority"])
                else:
                    names.add(name)
                    priorities[name] = str(rule["priority"])
            if share_type in ("relation", "all"):
                include_relations = True

        if not names:
            return []
        placeholders = ",".join("?" for _ in names)
        ordered_names = sorted(names)
        entity_rows = conn.execute(
            "SELECT id, name, entity_type, project FROM entities "
            f"WHERE name IN ({placeholders}) ORDER BY name",
            ordered_names,
        ).fetchall()
        entity_ids = [int(row["id"]) for row in entity_rows]
        observations: dict[int, list[dict]] = {
            entity_id: [] for entity_id in entity_ids
        }
        relations: dict[int, list[dict]] = {entity_id: [] for entity_id in entity_ids}
        if entity_ids:
            id_placeholders = ",".join("?" for _ in entity_ids)
            for row in conn.execute(
                "SELECT entity_id, content, created_at FROM observations "
                f"WHERE entity_id IN ({id_placeholders}) ORDER BY id",
                entity_ids,
            ).fetchall():
                observations[int(row["entity_id"])].append(
                    {"content": row["content"], "createdAt": row["created_at"]}
                )
            if include_relations:
                for row in conn.execute(
                    "SELECT r.from_id, et.name AS to_name, r.relation_type "
                    "FROM relations r JOIN entities et ON r.to_id = et.id "
                    f"WHERE r.from_id IN ({id_placeholders}) "
                    f"AND r.to_id IN ({id_placeholders}) "
                    "ORDER BY r.from_id, et.name, r.relation_type",
                    [*entity_ids, *entity_ids],
                ).fetchall():
                    relations[int(row["from_id"])].append(
                        {
                            "to": row["to_name"],
                            "relationType": row["relation_type"],
                        }
                    )

    shared_by = os.environ.get("GITHUB_USER", socket.gethostname())
    shared_at = now_iso()
    payload: list[dict] = []
    for row in entity_rows:
        entity_id = int(row["id"])
        obs = observations[entity_id]
        item = {
            "name": row["name"],
            "entityType": row["entity_type"],
            "project": row["project"],
            "observations": obs,
            "priority": priorities.get(str(row["name"]), "medium"),
            "sharedBy": shared_by,
            "sharedAt": shared_at,
            "sourceHash": source_hash(str(row["name"]), str(row["entity_type"]), obs),
        }
        if include_relations:
            item["relations"] = relations[entity_id]
        payload.append(item)
    return payload


def publish_peer_payloads(db_path: str, tasks: Iterable[dict]) -> dict:
    """Publish all configured peer payloads after the primary bridge push."""
    by_assignee: dict[str, list[dict]] = {}
    for task in tasks:
        assignee = str(task.get("assignee") or "").strip()
        if assignee:
            by_assignee.setdefault(assignee, []).append(task)

    assigned_recipients = 0
    for target, assigned_tasks in sorted(by_assignee.items()):
        try:
            if _clone_merge_push(
                target,
                "shared_tasks",
                assigned_tasks,
                _merge_tasks,
                f"bridge: shared {len(assigned_tasks)} tasks "
                f"from {socket.gethostname()} to {target}",
            ):
                assigned_recipients += 1
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.warning("Assigned-task push to %s failed: %s", target, exc)

    knowledge_shared = 0
    successful_targets: list[str] = []
    for target in _knowledge_targets(db_path):
        try:
            knowledge = _build_knowledge_payload(db_path, target)
            if knowledge and _clone_merge_push(
                target,
                "shared_knowledge",
                knowledge,
                _merge_knowledge,
                f"bridge: shared {len(knowledge)} entities "
                f"from {socket.gethostname()} to {target}",
            ):
                knowledge_shared += len(knowledge)
                successful_targets.append(target)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.warning("Knowledge push to %s failed: %s", target, exc)

    if successful_targets:
        synced_at = now_iso()
        with get_conn(db_path) as conn:
            conn.executemany(
                "UPDATE collaborators SET last_sync_at = ? WHERE github_user = ?",
                [(synced_at, target) for target in successful_targets],
            )
    return {
        "assigned_task_recipients": assigned_recipients,
        "knowledge_shared": knowledge_shared,
    }


def create_public_release(
    public_entities: list[dict],
    public_tasks: list[dict],
    machine_id: str,
) -> str | None:
    """Create the optional GitHub release for an already-pushed public payload."""
    if not public_entities and not public_tasks:
        return None
    repository = os.environ.get("BRIDGE_GH_REPO", "").strip()
    if not repository:
        log.warning("BRIDGE_GH_REPO not set; skipping public-knowledge release")
        return None
    tag_name = f"public-v{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    title = (
        f"Public Knowledge: {len(public_entities)} entities, {len(public_tasks)} tasks"
    )
    notes = (
        "## Public Knowledge Release\n\n"
        f"- **{len(public_entities)}** public entities\n"
        f"- **{len(public_tasks)}** public tasks\n\n"
        f"Published from `{machine_id}` at {now_iso()}"
    )
    try:
        result = subprocess.run(
            [
                "gh",
                "release",
                "create",
                tag_name,
                "--repo",
                repository,
                "--title",
                title,
                "--notes",
                notes,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            **_NOWIN,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("Public-knowledge release failed: %s", exc)
        return None
    if result.returncode != 0:
        log.warning("Public-knowledge release failed: %s", _git_detail(result))
        return None
    return tag_name
