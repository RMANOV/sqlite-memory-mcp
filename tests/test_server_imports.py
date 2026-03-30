"""Regression tests for core server module imports."""

import logging
import os
import sys

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


def test_server_exposes_sqlite3_for_fallback_handlers():
    assert hasattr(server, "sqlite3")


def test_tool_decorators_keep_fn_alias_for_direct_invocation_regressions():
    assert server.create_entities.fn is server.create_entities
    assert server.create_entities([]) == server.create_entities.__wrapped__([])
    assert task_server.create_task_or_note.fn is task_server.create_task_or_note
    assert callable(task_server.create_task_or_note.fn)
    assert session_server.session_save.fn is session_server.session_save
    assert bridge_server.bridge_push.fn is bridge_server.bridge_push
    assert collab_server.manage_collaborators.fn is collab_server.manage_collaborators
    assert entity_server.link_task_entity.fn is entity_server.link_task_entity
    assert intel_server.assess_context.fn is intel_server.assess_context


def test_unified_server_imports_and_exposes_mcp_instance():
    assert hasattr(unified_server, "mcp")


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
