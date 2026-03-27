"""Regression tests for core server module imports."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bridge_server
import collab_server
import entity_server
import intel_server
import server
import session_server
import task_server


def test_server_exposes_sqlite3_for_fallback_handlers():
    assert hasattr(server, "sqlite3")


def test_tool_decorators_keep_fn_alias_for_direct_invocation_regressions():
    assert server.create_entities.fn is server.create_entities
    assert task_server.create_task_or_note.fn is task_server.create_task_or_note
    assert session_server.session_save.fn is session_server.session_save
    assert bridge_server.bridge_push.fn is bridge_server.bridge_push
    assert collab_server.manage_collaborators.fn is collab_server.manage_collaborators
    assert entity_server.link_task_entity.fn is entity_server.link_task_entity
    assert intel_server.assess_context.fn is intel_server.assess_context
