import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import vec_search


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


def test_import_does_not_load_transformer_stack():
    repo = Path(__file__).resolve().parents[1]
    run = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; import vec_search; "
                "print(json.dumps({"
                "'sentence_transformers':'sentence_transformers' in sys.modules,"
                "'torch':'torch' in sys.modules"
                "}))"
            ),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(run.stdout) == {
        "sentence_transformers": False,
        "torch": False,
    }


def test_load_vec_returns_false_when_extension_load_fails(monkeypatch):
    calls = []

    class FakeConn:
        def execute(self, sql, params=()):
            assert sql == "SELECT vec_version()"
            raise sqlite3.OperationalError("no such function: vec_version")

        def enable_load_extension(self, enabled):
            calls.append(enabled)
            if enabled is False:
                raise sqlite3.OperationalError("disable failed")

    monkeypatch.setattr(vec_search, "_HAS_VEC", True)
    monkeypatch.setattr(
        vec_search,
        "sqlite_vec",
        types.SimpleNamespace(
            load=lambda conn: (_ for _ in ()).throw(
                sqlite3.OperationalError("load failed")
            )
        ),
        raising=False,
    )

    assert vec_search.load_vec(FakeConn()) is False
    assert calls == [True, False]


def test_vector_search_returns_empty_on_embedding_failure(monkeypatch):
    monkeypatch.setattr(vec_search, "VEC_AVAILABLE", True)
    monkeypatch.setattr(vec_search, "load_vec", lambda conn: True)
    monkeypatch.setattr(vec_search, "_model", object())
    monkeypatch.setattr(
        vec_search,
        "_embed_text_or_none",
        lambda text, context: None,
    )

    assert vec_search.vector_search(object(), "query") == []


def test_query_paths_do_not_wait_for_the_first_model_load(monkeypatch):
    # 14 Oct 2026: the first load ran inside search_nodes' read snapshot for
    # 4h14m, so no WAL checkpoint could run. Query paths must return at once.
    import threading

    started, release = threading.Event(), threading.Event()
    loads = []

    def slow_load():
        loads.append(1)
        started.set()
        release.wait(10)

    def no_embedding(text, context):
        raise AssertionError("query path embedded before the model was ready")

    monkeypatch.setattr(vec_search, "VEC_AVAILABLE", True)
    monkeypatch.setattr(vec_search, "load_vec", lambda conn: True)
    monkeypatch.setattr(vec_search, "_model", None)
    monkeypatch.setattr(vec_search, "_warmup_thread", None)
    monkeypatch.setattr(vec_search, "_get_model", slow_load)
    monkeypatch.setattr(vec_search, "_embed_text_or_none", no_embedding)
    try:
        assert vec_search.vector_search(object(), "query") == []
        assert vec_search.task_vector_search(object(), "query") == []
        assert started.wait(5)
        assert loads == [1]
    finally:
        release.set()
        vec_search._warmup_thread.join(5)


def test_query_path_embeds_once_the_model_is_ready(monkeypatch):
    seen = []

    class FakeConn:
        def execute(self, sql, params=()):
            seen.append(params)
            return _FakeCursor([{"eid": 1, "distance": 0.1}])

    monkeypatch.setattr(vec_search, "VEC_AVAILABLE", True)
    monkeypatch.setattr(vec_search, "load_vec", lambda conn: True)
    monkeypatch.setattr(vec_search, "_model", object())
    monkeypatch.setattr(vec_search, "_embed_text_or_none", lambda text, context: b"emb")

    assert vec_search.vector_search(FakeConn(), "query", limit=3) == [
        {"eid": 1, "distance": 0.1}
    ]
    assert seen == [(b"emb", 3)]


def test_vec_sync_task_returns_false_on_sqlite_error(monkeypatch):
    class FakeConn:
        def execute(self, sql, params=()):
            if sql.startswith("SELECT rowid, title"):
                return _FakeCursor(
                    [
                        {
                            "rowid": 7,
                            "title": "Task",
                            "description": None,
                            "notes": None,
                        }
                    ]
                )
            raise sqlite3.OperationalError("write failed")

    monkeypatch.setattr(vec_search, "VEC_AVAILABLE", True)
    monkeypatch.setattr(vec_search, "load_vec", lambda conn: True)
    monkeypatch.setattr(
        vec_search,
        "_embed_text_or_none",
        lambda text, context: b"embedding",
    )

    assert vec_search.vec_sync_task(FakeConn(), "task-1") is False


def test_backfill_task_embeddings_counts_only_successes(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO tasks (id, title) VALUES ('task-1', 'One')")
    conn.execute("INSERT INTO tasks (id, title) VALUES ('task-2', 'Two')")

    monkeypatch.setattr(vec_search, "VEC_AVAILABLE", True)
    monkeypatch.setattr(vec_search, "load_vec", lambda conn: True)
    monkeypatch.setattr(
        vec_search,
        "vec_sync_task",
        lambda conn, task_id: task_id == "task-1",
    )

    assert vec_search.backfill_task_embeddings(conn) == 1

    conn.close()


def test_backfill_embeddings_counts_only_successes(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO entities (name) VALUES ('One')")
    conn.execute("INSERT INTO entities (name) VALUES ('Two')")

    monkeypatch.setattr(vec_search, "VEC_AVAILABLE", True)
    monkeypatch.setattr(vec_search, "load_vec", lambda conn: True)
    monkeypatch.setattr(
        vec_search,
        "vec_sync_entity",
        lambda conn, entity_id: entity_id == 1,
    )

    assert vec_search.backfill_embeddings(conn) == 1

    conn.close()
