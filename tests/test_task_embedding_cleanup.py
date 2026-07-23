"""Derived task embeddings cannot survive authoritative task deletion."""

from __future__ import annotations

import sqlite3

import db_utils
import vec_search


def _fake_vec_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE task_embeddings (
            rowid INTEGER PRIMARY KEY,
            embedding BLOB
        );
        """
    )
    return conn


def test_prune_orphan_task_embeddings(monkeypatch):
    conn = _fake_vec_db()
    try:
        conn.execute(
            "INSERT INTO tasks(rowid,id,title,created_at,updated_at) "
            "VALUES (1,'live','Live','x','x')"
        )
        conn.executemany(
            "INSERT INTO task_embeddings(rowid,embedding) VALUES (?,X'00')",
            [(1,), (2,), (3,)],
        )
        monkeypatch.setattr(vec_search, "_HAS_VEC", True)
        monkeypatch.setattr(vec_search, "load_vec", lambda _conn: True)

        assert vec_search.prune_orphan_task_embeddings(conn) == 2
        assert [
            row[0] for row in conn.execute("SELECT rowid FROM task_embeddings")
        ] == [1]
        assert vec_search.prune_orphan_task_embeddings(conn) == 0
    finally:
        conn.close()


def test_taskdao_delete_cleans_embedding_before_task(monkeypatch):
    conn = _fake_vec_db()
    cleaned = []
    try:
        conn.execute(
            "INSERT INTO tasks(rowid,id,title,created_at,updated_at) "
            "VALUES (7,'target','Target','x','x')"
        )

        def cleanup(check_conn, rowid):
            assert check_conn.execute(
                "SELECT 1 FROM tasks WHERE rowid=?", (rowid,)
            ).fetchone()
            cleaned.append(rowid)
            return True

        monkeypatch.setattr(db_utils, "_remove_task_embedding_rowid_safe", cleanup)

        assert db_utils.TaskDAO.delete(conn, "target") == 1
        assert cleaned == [7]
        assert conn.execute("SELECT 1 FROM tasks").fetchone() is None
    finally:
        conn.close()
