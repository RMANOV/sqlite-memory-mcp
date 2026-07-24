"""Task embeddings must never extend the authoritative SQLite write lock."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading

import task_server
import vec_search


def test_create_schedules_embedding_only_after_commit(monkeypatch):
    events: list[str] = []

    @contextlib.contextmanager
    def fake_conn():
        events.append("begin")
        yield object()
        events.append("commit")

    monkeypatch.setattr(task_server, "_get_write_conn", fake_conn)
    monkeypatch.setattr(
        task_server,
        "_create_task_with_ledger",
        lambda *args, **kwargs: events.append("create"),
    )
    monkeypatch.setattr(
        task_server,
        "_vec_sync_task_safe",
        lambda task_id: events.append(f"schedule:{task_id}"),
    )

    result = json.loads(task_server.create_task_or_note.fn(title="lock regression"))

    assert events[:3] == ["begin", "create", "commit"]
    assert events[3] == f"schedule:{result['task_id']}"


def test_embedding_scheduler_failure_cannot_reverse_committed_task(monkeypatch):
    monkeypatch.setattr(
        task_server._task_embedding_scheduler,
        "request",
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
    )

    task_server._vec_sync_task_safe("already-committed")


def test_detached_embedding_holds_no_write_lock_during_inference(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            notes TEXT
        );
        CREATE TABLE task_embeddings (embedding BLOB);
        INSERT INTO tasks(id, title, description, notes)
        VALUES ('task-1', 'Title', 'Description', 'Notes');
        """
    )
    conn.commit()
    conn.close()

    inference_started = threading.Event()
    release_inference = threading.Event()

    def blocked_embed(_text):
        inference_started.set()
        assert release_inference.wait(5)
        return b"embedding"

    monkeypatch.setattr(vec_search, "VEC_AVAILABLE", True)
    monkeypatch.setattr(vec_search, "load_vec", lambda _conn: True)
    monkeypatch.setattr(vec_search, "embed_text", blocked_embed)

    outcome: list[bool] = []
    worker = threading.Thread(
        target=lambda: outcome.append(
            vec_search.vec_sync_task_detached(str(db_path), "task-1")
        )
    )
    worker.start()
    assert inference_started.wait(5)

    contender = sqlite3.connect(db_path, timeout=1, isolation_level=None)
    try:
        contender.execute("BEGIN IMMEDIATE")
        contender.execute("ROLLBACK")
    finally:
        contender.close()

    release_inference.set()
    worker.join(5)
    assert not worker.is_alive()
    assert outcome == [True]

    verify = sqlite3.connect(db_path)
    try:
        assert (
            verify.execute("SELECT embedding FROM task_embeddings").fetchone()[0]
            == b"embedding"
        )
    finally:
        verify.close()
