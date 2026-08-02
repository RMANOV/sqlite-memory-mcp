"""B4: publishing a backup must never be able to leave the operator worse off.

Two properties of ``create_backup`` are pinned here, both about the moment a
verified staging directory becomes a published generation.

**Collision.** Generations are named by the second, so two runs in the same
second collide — the ordinary case for a scripted or retried backup. The old
publish handled that by ``shutil.rmtree``-ing the colliding generation and then
renaming over the hole. Between those two calls a verified backup was gone and
its replacement was not yet in place: a crash, a full disk or an EXDEV there
left the operator with one *fewer* backup than before they ran a backup. The
whole point of the operation is the opposite.

**WAL recovery.** ``create_backup`` opens the source ``mode=ro``, but a crashed
writer can leave a WAL that must be recovered before it can be read, and
recovery is a write. One retry with a writable handle exists for that, and it is
the only place this function can touch the operator's database. It had no test
at all — a hot 4.1 MB WAL never triggered it — so it is exercised here by
injecting the exact error SQLite raises, and the source's *contents* are
compared before and after so the cost of that one writable handle is on record
rather than assumed.

Every test builds its own temp database; nothing reads or writes ~/.claude.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

import db_utils
from db_utils import create_backup, list_backups, verify_backup

FIXED = datetime(2026, 8, 2, 19, 42, 54, tzinfo=timezone.utc)


def _make_db(path, *, rows: int = 20, checkpoint: bool = True) -> None:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)")
    conn.executemany(
        "INSERT INTO tasks(title) VALUES (?)", [(f"title {i}",) for i in range(rows)]
    )
    if checkpoint:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.close()


def _sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _rows(path) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT id, title FROM tasks ORDER BY id").fetchall()
    finally:
        conn.close()


# ── collision ─────────────────────────────────────────────────────────────


def test_a_failed_publish_never_destroys_the_previous_generation(tmp_path, monkeypatch):
    """The crash window, made deterministic.

    ``os.replace`` is the last step of the publish. Failing exactly there is
    what a crash, a full disk or a cross-device staging directory looks like
    from the caller's side.
    """
    src = tmp_path / "memory.db"
    _make_db(src)
    root = tmp_path / "b"

    first = create_backup(str(src), backup_dir=str(root), now=FIXED)
    first_sha = _sha(first["backup_path"])

    real_replace = os.replace

    def exploding_replace(a, b, *args, **kwargs):
        # Only the publish; anything else in the process keeps working.
        if str(b).startswith(str(root)):
            raise OSError(18, "Invalid cross-device link")
        return real_replace(a, b, *args, **kwargs)

    monkeypatch.setattr(db_utils.os, "replace", exploding_replace)

    with pytest.raises(OSError):
        create_backup(str(src), backup_dir=str(root), now=FIXED)  # same second

    assert os.path.exists(first["backup_path"]), (
        "the previous generation was deleted to make room and the replacement "
        "never landed — the operator now has fewer backups than before"
    )
    assert _sha(first["backup_path"]) == first_sha
    assert verify_backup(first["path"])["ok"] is True
    assert list_backups(str(root)) == [first["path"]], (
        "a half-published generation was left visible to retention"
    )


def test_two_backups_in_the_same_second_both_survive(tmp_path):
    src = tmp_path / "memory.db"
    _make_db(src)
    root = tmp_path / "b"

    first = create_backup(str(src), backup_dir=str(root), now=FIXED)
    second = create_backup(str(src), backup_dir=str(root), now=FIXED)

    assert first["path"] != second["path"], (
        "the second run took the first one's name — a verified backup was "
        "destroyed to publish an identical one"
    )
    assert verify_backup(first["path"])["ok"] is True
    assert verify_backup(second["path"])["ok"] is True
    assert len(list_backups(str(root))) == 2


def test_concurrent_same_process_backups_use_distinct_staging(tmp_path, monkeypatch):
    """PID + timestamp is not unique when two scheduler threads overlap."""
    src = tmp_path / "memory.db"
    _make_db(src, rows=200)
    root = tmp_path / "b"
    barrier = threading.Barrier(2)
    seen: list[str] = []
    seen_lock = threading.Lock()
    real_backup_to = db_utils._backup_to

    def synchronized_backup(src_conn, dest_path, pages):
        with seen_lock:
            seen.append(dest_path)
        # Hold both calls after staging allocation. With the old deterministic
        # path the second call had already rmtree'd the first call's directory,
        # and both writers now targeted the same memory.db.
        barrier.wait(timeout=20)
        return real_backup_to(src_conn, dest_path, pages)

    monkeypatch.setattr(db_utils, "_backup_to", synchronized_backup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(create_backup, str(src), backup_dir=str(root), now=FIXED)
            for _ in range(2)
        ]
        made = [future.result(timeout=60) for future in futures]

    assert len(seen) == 2 and len(set(seen)) == 2, (
        "concurrent calls shared one staging database: " + repr(seen)
    )
    assert len({item["path"] for item in made}) == 2
    assert all(verify_backup(item["path"])["ok"] for item in made)


def test_a_disambiguated_generation_is_a_first_class_generation(tmp_path):
    """It must be listable, verifiable, prunable and honestly self-described."""
    src = tmp_path / "memory.db"
    _make_db(src)
    root = tmp_path / "b"

    create_backup(str(src), backup_dir=str(root), now=FIXED, generations=3)
    second = create_backup(str(src), backup_dir=str(root), now=FIXED, generations=3)

    assert second["generation"] == os.path.basename(second["path"]), (
        "the manifest names a directory that is not the one it lives in"
    )
    assert second["path"] in list_backups(str(root))
    assert second["path"] in second["generations_kept"]

    # Retention still bounds the directory, and sorts the same-second pair in
    # the order they were written.
    for _ in range(3):
        create_backup(str(src), backup_dir=str(root), now=FIXED, generations=3)
    kept = list_backups(str(root))
    assert len(kept) == 3
    assert kept == sorted(kept)


def test_a_foreign_directory_on_the_name_is_stepped_around_not_deleted(tmp_path):
    src = tmp_path / "memory.db"
    _make_db(src)
    root = tmp_path / "b"
    root.mkdir()
    squatter = root / FIXED.strftime("%Y%m%dT%H%M%SZ")
    squatter.mkdir()
    (squatter / "operator-notes.txt").write_text("do not delete", encoding="utf-8")

    made = create_backup(str(src), backup_dir=str(root), now=FIXED)

    assert (squatter / "operator-notes.txt").read_text(encoding="utf-8") == "do not delete"
    assert made["path"] != str(squatter)
    assert verify_backup(made["path"])["ok"] is True


# ── WAL recovery retry ────────────────────────────────────────────────────


def _inject_readonly_once(monkeypatch, probe: list | None = None):
    """Make the first ``_backup_to`` raise what SQLite raises on an unrecovered WAL."""
    real = db_utils._backup_to
    calls: list[int] = []

    def flaky(src_conn, dest_path, pages):
        calls.append(1)
        if probe is not None:
            try:
                src_conn.execute("PRAGMA user_version = 4242")
                probe.append(True)
            except sqlite3.OperationalError:
                probe.append(False)
        if len(calls) == 1:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real(src_conn, dest_path, pages)

    monkeypatch.setattr(db_utils, "_backup_to", flaky)
    return calls


def test_the_wal_recovery_retry_escalates_from_readonly_to_writable(tmp_path, monkeypatch):
    """Proves both halves of the branch's claim, not just that it retried."""
    src = tmp_path / "memory.db"
    _make_db(src)
    root = tmp_path / "b"

    writable: list[bool] = []
    calls = _inject_readonly_once(monkeypatch, probe=writable)

    manifest = create_backup(str(src), backup_dir=str(root), now=FIXED)

    assert len(calls) == 2, "the readonly marker did not trigger the retry"
    assert writable == [False, True], (
        "first handle must be genuinely read-only and the retry genuinely "
        f"writable; observed {writable}"
    )
    assert manifest["quick_check_ok"] is True
    assert verify_backup(manifest["path"])["ok"] is True

    # The copy came from the writable handle, not a stale one: the probe's
    # write is visible in the backup.
    conn = sqlite3.connect(manifest["backup_path"])
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4242
    finally:
        conn.close()


def test_the_retry_leaves_the_sources_contents_intact(tmp_path, monkeypatch):
    """What that one writable handle costs, measured rather than assumed."""
    src = tmp_path / "memory.db"
    _make_db(src, checkpoint=False)  # hot WAL: the state the retry exists for
    hot = sqlite3.connect(str(src), isolation_level=None)
    hot.execute("INSERT INTO tasks(title) VALUES ('uncheckpointed')")
    assert os.path.exists(str(src) + "-wal"), "premise broken: no hot WAL"

    before_rows = _rows(src)
    source_artifacts = [src, tmp_path / "memory.db-wal", tmp_path / "memory.db-shm"]
    assert all(path.is_file() for path in source_artifacts), (
        "premise broken: writable-recovery receipt requires db/wal/shm"
    )
    before_bytes = {
        path.name: (path.stat().st_size, _sha(path)) for path in source_artifacts
    }
    root = tmp_path / "b"

    calls = _inject_readonly_once(monkeypatch)
    manifest = create_backup(str(src), backup_dir=str(root), now=FIXED)
    after_bytes = {
        path.name: (path.stat().st_size, _sha(path)) for path in source_artifacts
    }
    hot.close()

    assert len(calls) == 2
    assert after_bytes == before_bytes, (
        "the writable WAL-recovery retry changed source db/wal/shm bytes: "
        f"before={before_bytes!r} after={after_bytes!r}"
    )
    assert _rows(src) == before_rows, "the writable retry changed the source data"
    assert _rows(manifest["backup_path"]) == before_rows, (
        "the backup does not match the source it was taken from"
    )
    assert verify_backup(manifest["path"])["ok"] is True


def test_a_readonly_error_that_is_not_a_wal_problem_still_fails(tmp_path, monkeypatch):
    """The retry must not become a blanket 'open it writable and try again'."""
    src = tmp_path / "memory.db"
    _make_db(src)
    root = tmp_path / "b"

    def always_broken(src_conn, dest_path, pages):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db_utils, "_backup_to", always_broken)

    with pytest.raises(sqlite3.OperationalError):
        create_backup(str(src), backup_dir=str(root), now=FIXED)

    assert list_backups(str(root)) == []
    assert not [p for p in root.iterdir() if not p.name.startswith(".")], (
        f"a failed backup left debris behind: {list(root.iterdir())}"
    )
