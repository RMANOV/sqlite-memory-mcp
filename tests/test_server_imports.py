"""Regression tests for core server module imports."""

import logging
import os
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bridge_server
import collab_server
import db_utils
import entity_server
import intel_server
import server
import session_server
import task_server
import unified_server


_ROOT = Path(__file__).resolve().parents[1]


def test_server_exposes_sqlite3_for_fallback_handlers():
    assert hasattr(server, "sqlite3")


def test_tool_decorators_keep_fn_alias_for_direct_invocation_regressions():
    assert server.create_entities.fn is server.create_entities
    assert server.create_entities([]) == server.create_entities.__wrapped__([])
    assert task_server.create_task_or_note.fn is task_server.create_task_or_note
    assert (
        task_server.upsert_note_by_title_project.fn
        is task_server.upsert_note_by_title_project
    )
    assert task_server.find_by_title.fn is task_server.find_by_title
    assert callable(task_server.create_task_or_note.fn)
    assert session_server.session_save.fn is session_server.session_save
    assert bridge_server.bridge_push.fn is bridge_server.bridge_push
    assert bridge_server.bridge_doctor.fn is bridge_server.bridge_doctor
    assert collab_server.manage_collaborators.fn is collab_server.manage_collaborators
    assert entity_server.link_task_entity.fn is entity_server.link_task_entity
    assert intel_server.assess_context.fn is intel_server.assess_context
    assert intel_server.audit_memory.fn is intel_server.audit_memory
    assert intel_server.replay_memory.fn is intel_server.replay_memory
    assert intel_server.govern_fact.fn is intel_server.govern_fact


def test_unified_server_imports_and_exposes_mcp_instance():
    assert hasattr(unified_server, "mcp")


def test_project_scripts_call_main_wrappers():
    with (_ROOT / "pyproject.toml").open("rb") as fh:
        scripts = tomllib.load(fh)["project"]["scripts"]

    assert scripts["sqlite-memory-mcp"] == "server:main"
    assert scripts["sqlite-memory-core"] == "server:main"
    assert scripts["sqlite-memory-session"] == "session_server:main"
    assert scripts["sqlite-memory-tasks"] == "task_server:main"
    assert scripts["sqlite-memory-bridge"] == "bridge_server:main"
    assert scripts["sqlite-memory-collab"] == "collab_server:main"
    assert scripts["sqlite-memory-entity"] == "entity_server:main"
    assert scripts["sqlite-memory-intel"] == "intel_server:main"
    assert scripts["sqlite-memory-unified"] == "unified_server:main"


@pytest.mark.parametrize(
    ("module", "server_name", "startup_hook", "hook_label"),
    [
        (server, "sqlite-kb", "_migrate_jsonl", "migrate"),
        (session_server, "sqlite-session", None, None),
        (task_server, "sqlite-tasks", None, None),
        (bridge_server, "sqlite-bridge", None, None),
        (collab_server, "sqlite-collab", None, None),
        (entity_server, "sqlite-entity", None, None),
        (intel_server, "sqlite-intel", None, None),
        (unified_server, "sqlite-unified", "ensure_db_initialized", "ensure_db"),
    ],
    ids=lambda value: (
        getattr(value, "__name__", value) if value is not None else "none"
    ),
)
def test_server_main_wrappers_preserve_startup_hooks(
    monkeypatch,
    module,
    server_name,
    startup_hook,
    hook_label,
):
    calls = []

    if startup_hook:
        monkeypatch.setattr(
            module,
            startup_hook,
            lambda: calls.append((hook_label,)),
        )
    monkeypatch.setattr(
        module,
        "maybe_mount_premium_extensions",
        lambda mounted_mcp, server_name=server_name: calls.append(
            ("mount", server_name, mounted_mcp)
        ),
    )
    monkeypatch.setattr(
        module.mcp,
        "run",
        lambda transport="stdio": calls.append(("run", transport)),
    )

    module.main()

    expected = []
    if hook_label:
        expected.append((hook_label,))
    expected.append(("mount", server_name, module.mcp))
    expected.append(("run", "stdio"))
    assert calls == expected


def test_task_server_instructions_make_description_the_default_body_field():
    assert "description as the default primary body" in task_server.mcp.instructions
    assert "Use find_by_title when only a remembered phrase is known" in (
        task_server.mcp.instructions
    )
    assert "Use upsert_note_by_title_project for idempotent research/decision notes" in (
        task_server.mcp.instructions
    )
    assert "confidence gating" in task_server.mcp.instructions
    assert "main long-form task/note text in ``description`` by default" in (
        task_server.create_task_or_note.description or ""
    )
    assert "Secondary/internal notes or machine-readable metadata." in (
        task_server.create_task_or_note.description or ""
    )
    assert "put the main long-form body in description by default" in (
        unified_server.mcp.instructions
    )
    assert "title/name, description, notes, observations" in (
        unified_server.mcp.instructions
    )
    assert "confidence gating" in unified_server.mcp.instructions


def test_setup_logger_falls_back_when_primary_log_path_is_unwritable(monkeypatch):
    logger_name = "sqlite-test-fallback-logger"
    logger = logging.getLogger(logger_name)
    logger.handlers.clear()

    original_file_handler = logging.FileHandler
    calls = {"count": 0}

    def flaky_file_handler(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError("denied")
        return original_file_handler(*args, **kwargs)

    monkeypatch.setattr(logging, "FileHandler", flaky_file_handler)

    logger = db_utils.setup_logger(logger_name, "fallback-test.log")

    assert logger.handlers
    assert calls["count"] >= 2
