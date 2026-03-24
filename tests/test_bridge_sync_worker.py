"""Focused tests for bridge sync safety checks."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bridge_sync_worker import _check_sync_safety


@pytest.fixture
def setup(tmp_path):
    db_path = str(tmp_path / "test.db")
    bridge_dir = str(tmp_path / "bridge")
    os.makedirs(os.path.join(bridge_dir, "tasks"), exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT NULL,
            notes TEXT DEFAULT NULL
        )
        """
    )
    yield conn, bridge_dir
    conn.close()


def test_check_sync_safety_flags_drastic_description_shrink(setup):
    conn, bridge_dir = setup
    task_id = "task-001"
    conn.execute(
        "INSERT INTO tasks (id, title, description, notes) VALUES (?, ?, ?, ?)",
        (task_id, "Big task", "x" * 200, None),
    )
    with open(
        os.path.join(bridge_dir, "tasks", f"{task_id}.json"),
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(
            {
                "id": task_id,
                "title": "Big task",
                "description": "x" * 2400,
                "notes": None,
            },
            fh,
        )

    safety = _check_sync_safety(conn, bridge_dir)

    assert safety["is_safe"] is False
    assert safety["descriptions_shrunk"] == 1
    assert safety["examples"][0]["task_id"] == task_id
