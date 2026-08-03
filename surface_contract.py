"""Machine-readable surface contracts for bridge/task persistence paths.

These contracts intentionally mirror live storage surfaces so new fields or
artifacts cannot quietly exist in only one part of the sync/query/export graph.
"""

from __future__ import annotations

from typing import Any

from db_utils import CONTENT_FIELDS, EXTENDED_MEMORY_KEYS, METADATA_FIELDS

SURFACE_CONTRACT_VERSION = "surface_contract_v1"

# Explicit contract: one entry per current persisted task field.
TASK_FIELD_SURFACE_CONTRACT: dict[str, dict[str, bool]] = {
    "id": {
        "create": True,
        "edit": False,
        "query": True,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "title": {
        "create": True,
        "edit": True,
        "query": True,
        "search": True,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "status": {
        "create": True,
        "edit": True,
        "query": True,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "priority": {
        "create": True,
        "edit": True,
        "query": True,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "section": {
        "create": True,
        "edit": True,
        "query": True,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "due_date": {
        "create": True,
        "edit": True,
        "query": True,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "project": {
        "create": True,
        "edit": True,
        "query": True,
        "search": True,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "parent_id": {
        "create": True,
        "edit": True,
        "query": True,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "recurring": {
        "create": True,
        "edit": True,
        "query": False,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "reminder_at": {
        "create": True,
        "edit": True,
        "query": False,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "type": {
        "create": True,
        "edit": True,
        "query": False,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "assignee": {
        "create": False,
        "edit": True,
        "query": False,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "shared_by": {
        "create": False,
        "edit": True,
        "query": False,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "visibility": {
        "create": False,
        "edit": True,
        "query": False,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "publish_requested_at": {
        "create": False,
        "edit": True,
        "query": False,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "created_at": {
        "create": True,
        "edit": False,
        "query": True,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "updated_at": {
        "create": True,
        "edit": True,
        "query": True,
        "search": False,
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "description": {
        "create": True,
        "edit": True,
        "query": True,
        "search": True,
        "bridge_index": False,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "notes": {
        "create": True,
        "edit": True,
        "query": True,
        "search": True,
        "bridge_index": False,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
}

TASK_HYDRATION_SURFACE_CONTRACT: dict[str, dict[str, bool]] = {
    "_attachments": {
        "bridge_index": False,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "_field_ts": {
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "_links": {
        "bridge_index": False,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "_link_tombstones": {
        "bridge_index": False,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
    "_tombstone": {
        "bridge_index": True,
        "bridge_task_file": True,
        "import": True,
        "bootstrap": True,
    },
}

EXTENDED_MEMORY_SURFACE_CONTRACT: dict[str, dict[str, Any]] = {
    key: {
        "bridge_file": f"extended_memory/{key}.json",
        "export": True,
        "import": True,
        "bootstrap": True,
        "git_stage": True,
        "pages_publish": False,
    }
    for key in EXTENDED_MEMORY_KEYS
}

BRIDGE_ARTIFACT_SURFACE_CONTRACT: dict[str, dict[str, Any]] = {
    "shared.json": {
        "export": True,
        "pull": True,
        "bootstrap": False,
        "git_stage": True,
        "pages_publish": True,
        "legacy_fallback": True,
    },
    "shared.js": {
        "export": True,
        "pull": False,
        "bootstrap": False,
        "git_stage": True,
        "pages_publish": True,
        "legacy_fallback": False,
    },
    "index.json": {
        "export": True,
        "pull": True,
        "bootstrap": True,
        "git_stage": True,
        "pages_publish": True,
        "legacy_fallback": False,
    },
    "tasks/": {
        "export": True,
        "pull": True,
        "bootstrap": True,
        "git_stage": True,
        "pages_publish": True,
        "legacy_fallback": False,
    },
    "attachments/": {
        "export": True,
        "pull": True,
        "bootstrap": True,
        "git_stage": True,
        "pages_publish": True,
        "legacy_fallback": False,
    },
    "entities/": {
        "export": True,
        "pull": True,
        "bootstrap": True,
        "git_stage": True,
        "pages_publish": True,
        "legacy_fallback": False,
    },
    "entities_index.json": {
        "export": True,
        "pull": True,
        "bootstrap": True,
        "git_stage": True,
        "pages_publish": True,
        "legacy_fallback": False,
    },
    "extended_memory/": {
        "export": True,
        "pull": True,
        "bootstrap": True,
        "git_stage": True,
        "pages_publish": False,
        "legacy_fallback": False,
    },
    # Render-only Kanban payload (preview of tasks). Tracked/synced so it opens
    # immediately on peers, but pull=False -> NEVER imported back into the DB
    # (full bodies live in shared.json/tasks/* transport, which stay untruncated).
    "kanban_payload.json": {
        "export": True,
        "pull": False,
        "bootstrap": False,
        "git_stage": True,
        "pages_publish": True,
        "legacy_fallback": False,
    },
}

BRIDGE_GIT_STAGE_PATHS = tuple(
    path
    for path, spec in BRIDGE_ARTIFACT_SURFACE_CONTRACT.items()
    if spec.get("git_stage")
)

BRIDGE_PAGES_PUBLISH_PATHS = tuple(
    path
    for path, spec in BRIDGE_ARTIFACT_SURFACE_CONTRACT.items()
    if spec.get("pages_publish")
)

BRIDGE_SHARED_PAYLOAD_KEYS = (
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
    "ui_profiles",
) + tuple(EXTENDED_MEMORY_KEYS)


def build_surface_contract_report() -> dict[str, Any]:
    """Return a machine-readable report suitable for tests and doctor tools."""
    return {
        "version": SURFACE_CONTRACT_VERSION,
        "task_fields": TASK_FIELD_SURFACE_CONTRACT,
        "task_field_order": list(METADATA_FIELDS) + list(CONTENT_FIELDS),
        "task_hydration": TASK_HYDRATION_SURFACE_CONTRACT,
        "bridge_artifacts": BRIDGE_ARTIFACT_SURFACE_CONTRACT,
        "bridge_git_stage_paths": list(BRIDGE_GIT_STAGE_PATHS),
        "bridge_pages_publish_paths": list(BRIDGE_PAGES_PUBLISH_PATHS),
        "bridge_shared_payload_keys": list(BRIDGE_SHARED_PAYLOAD_KEYS),
        "extended_memory": EXTENDED_MEMORY_SURFACE_CONTRACT,
    }
