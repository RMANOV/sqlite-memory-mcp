"""Regression tests for core server module imports."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import server


def test_server_exposes_sqlite3_for_fallback_handlers():
    assert hasattr(server, "sqlite3")
