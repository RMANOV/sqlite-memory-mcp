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