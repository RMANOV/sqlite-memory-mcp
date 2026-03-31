import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import tray_dialogs


class _BrokenTruthScoreConn:
    def execute(self, sql, params=()):
        raise sqlite3.OperationalError("knowledge_ratings unavailable")


class _BrokenTruthScoreConnCtx:
    def __enter__(self):
        return _BrokenTruthScoreConn()

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def test_batch_truth_scores_returns_stale_cache_on_sqlite_error(monkeypatch):
    stale_cache = {"EntityA": "🟡 "}
    monkeypatch.setattr(tray_dialogs, "_ts_cache", stale_cache.copy())
    monkeypatch.setattr(tray_dialogs, "_ts_cache_time", 0.0)
    monkeypatch.setattr(
        tray_dialogs,
        "get_conn",
        lambda db_path=None: _BrokenTruthScoreConnCtx(),
    )

    assert tray_dialogs._batch_truth_scores() == stale_cache


def test_should_render_context_preview_hides_weak_executor_pack():
    assert (
        tray_dialogs._should_render_context_preview(
            {
                "items_included": 1,
                "body": "## Context Fragments\nweak raw chunk",
                "previewable": False,
            }
        )
        is False
    )


def test_should_render_context_preview_allows_strong_executor_pack():
    assert (
        tray_dialogs._should_render_context_preview(
            {
                "items_included": 1,
                "body": "## Canonical Facts\nhigh-confidence fact",
                "previewable": True,
            }
        )
        is True
    )
