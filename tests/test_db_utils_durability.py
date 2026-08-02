"""Durability, log-bounding and language-agnostic tokenisation for db_utils.

Every test builds its own temp database or temp HOME, and tests/conftest.py
redirects HOME for the whole session besides. Nothing here reads or writes
~/.claude/memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import random
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import db_utils
from db_utils import (
    BOOTSTRAP_STOPWORDS,
    BackupError,
    build_df_stopwords,
    create_backup,
    inspect_database,
    learn_similarity_stopwords,
    list_backups,
    set_similarity_stopwords,
    setup_logger,
    tokenize_for_similarity,
    verify_backup,
)

# ── helpers ───────────────────────────────────────────────────────────────


def _make_db(path, *, rows: int = 500, checkpoint: bool = True) -> sqlite3.Connection:
    """A WAL database with two tables, an index, a view and a trigger."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, body TEXT)")
    conn.execute('CREATE TABLE "odd name" (v TEXT)')
    conn.execute("CREATE INDEX ix_tasks_title ON tasks(title)")
    conn.execute("CREATE VIEW v_tasks AS SELECT id FROM tasks")
    conn.execute(
        "CREATE TRIGGER trg_tasks AFTER INSERT ON tasks BEGIN "
        'INSERT INTO "odd name"(v) VALUES (NEW.title); END'
    )
    conn.executemany(
        "INSERT INTO tasks(title, body) VALUES (?, ?)",
        [(f"title {i}", f"body {i}") for i in range(rows)],
    )
    if checkpoint:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    return conn


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# Written and then abandoned with os._exit: no close, so no close-time
# checkpoint. The -wal is left full and hot, which is what a SIGKILLed or
# crashed MCP server leaves behind and exactly when a backup matters most.
_CRASHING_WRITER = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, body TEXT)")
conn.execute('CREATE TABLE "odd name" (v TEXT)')
conn.executemany(
    "INSERT INTO tasks(title, body) VALUES (?, ?)",
    [("title %d" % i, "body %d" % i) for i in range(int(sys.argv[2]))],
)
os._exit(0)
"""


def _hot_wal_db(path, *, rows: int = 4000):
    """Build a database whose -wal is full and whose writer is gone.

    A live second connection would *not* reproduce the hazard: SQLite only
    checkpoints when the closing handle is the last one on the database, so a
    test that keeps a writer open passes whether the reader is read-only or not.
    """
    subprocess.run(
        [sys.executable, "-c", _CRASHING_WRITER, str(path), str(rows)],
        check=True,
        timeout=120,
        capture_output=True,
    )
    wal = path.parent / (path.name + "-wal")
    assert wal.stat().st_size > 0, "no hot WAL: this test cannot discriminate"
    return wal


# ── Change 1: backups ─────────────────────────────────────────────────────


def test_backup_produces_verified_generation(tmp_path):
    src = tmp_path / "memory.db"
    conn = _make_db(src)
    conn.close()

    manifest = create_backup(str(src), backup_dir=str(tmp_path / "b"))

    assert manifest["quick_check"] == ["ok"]
    assert manifest["row_counts"] == {"tasks": 500, "odd name": 500}
    assert manifest["total_rows"] == 1000
    # 2 tables + 1 index + 1 view + 1 trigger (autoindexes excluded)
    assert manifest["schema_objects"] == 5
    assert manifest["schema_objects_by_type"] == {
        "index": 1,
        "table": 2,
        "trigger": 1,
        "view": 1,
    }
    assert manifest["unreadable_tables"] == {}
    assert len(manifest["sha256"]) == 64

    backup_file = tmp_path / "b" / manifest["generation"] / "memory.db"
    assert backup_file.is_file()
    assert json.loads(
        (backup_file.parent / "manifest.json").read_text(encoding="utf-8")
    )["total_rows"] == 1000
    # Single self-contained file — no WAL sidecars to forget when restoring.
    # Their absence proves nothing on its own: SQLite removes them on a clean
    # last-connection close whatever the journal mode is, so this assertion held
    # with the `PRAGMA journal_mode=DELETE` deleted. The mode recorded *in the
    # artifact* is what actually changes.
    probe = db_utils._connect_readonly(str(backup_file))
    try:
        assert probe.execute("PRAGMA journal_mode;").fetchone()[0] == "delete"
    finally:
        probe.close()
    assert not (backup_file.parent / "memory.db-wal").exists()
    assert not (backup_file.parent / "memory.db-shm").exists()

    assert verify_backup(str(backup_file.parent))["ok"] is True


def test_backup_uses_0600_files_in_a_0700_directory(tmp_path):
    src = tmp_path / "memory.db"
    _make_db(src, rows=10).close()
    root = tmp_path / "b"

    manifest = create_backup(str(src), backup_dir=str(root))
    gen = root / manifest["generation"]

    assert oct(gen.stat().st_mode & 0o777) == "0o700"
    assert oct((gen / "memory.db").stat().st_mode & 0o777) == "0o600"
    assert oct((gen / "manifest.json").stat().st_mode & 0o777) == "0o600"
    assert oct(root.stat().st_mode & 0o777) == "0o700"


def test_backup_captures_uncheckpointed_wal_unlike_a_file_copy(tmp_path):
    """The reason this is Connection.backup() and not shutil.copy."""
    src = tmp_path / "memory.db"
    conn = _make_db(src, rows=100)
    # Leave 400 rows sitting in the -wal with no checkpoint.
    conn.executemany(
        "INSERT INTO tasks(title, body) VALUES (?, ?)",
        [(f"wal {i}", f"wal {i}") for i in range(400)],
    )
    assert (tmp_path / "memory.db-wal").stat().st_size > 0

    naive = tmp_path / "naive-copy.db"
    naive.write_bytes(src.read_bytes())  # what shutil.copy of the .db alone gives

    manifest = create_backup(str(src), backup_dir=str(tmp_path / "b"))
    conn.close()

    naive_conn = sqlite3.connect(str(naive))
    naive_rows = naive_conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    naive_conn.close()

    assert manifest["row_counts"]["tasks"] == 500
    assert naive_rows < 500, "byte copy unexpectedly complete; test is not proving much"


def test_backup_is_consistent_with_a_live_writer_and_never_mutates_source(tmp_path):
    src = tmp_path / "memory.db"
    _make_db(src, rows=200).close()

    stop = threading.Event()
    errors: list[Exception] = []

    def writer() -> None:
        w = sqlite3.connect(str(src), isolation_level=None, timeout=30)
        w.execute("PRAGMA journal_mode=WAL;")
        n = 0
        try:
            while not stop.is_set():
                w.execute("INSERT INTO tasks(title, body) VALUES (?, ?)", (n, n))
                n += 1
                time.sleep(0.001)
        except Exception as exc:  # pragma: no cover - surfaced via assert below
            errors.append(exc)
        finally:
            w.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        time.sleep(0.05)
        manifest = create_backup(str(src), backup_dir=str(tmp_path / "b"))
    finally:
        stop.set()
        thread.join()

    assert not errors, f"backup blocked the live writer: {errors}"
    assert manifest["quick_check"] == ["ok"]
    # Trigger invariant holds -> the snapshot is transactionally consistent.
    assert manifest["row_counts"]["tasks"] == manifest["row_counts"]["odd name"]
    assert manifest["row_counts"]["tasks"] >= 200


def test_backup_leaves_a_hot_wal_source_byte_identical(tmp_path):
    """``PRAGMA query_only=ON`` does not make a handle non-mutating; mode=ro does.

    query_only rejects SQL writes, but the handle is still a full member of the
    WAL, so closing it as the last connection runs SQLite's checkpoint: the
    source database is rewritten and -wal/-shm are unlinked. A backup silently
    mutating the database it is reading.

    The previous version of this test checkpointed the source to empty before
    backing it up, so there was nothing for the close-time checkpoint to write
    back and it passed with the read-only handle removed entirely.
    """
    src = tmp_path / "memory.db"
    wal = _hot_wal_db(src, rows=4000)

    before_db, before_wal = _sha256(src), _sha256(wal)
    before_wal_size = wal.stat().st_size

    manifest = create_backup(str(src), backup_dir=str(tmp_path / "b"))

    assert src.is_file(), "the backup deleted its own source"
    assert wal.is_file(), "the backup checkpointed and unlinked the source -wal"
    assert _sha256(src) == before_db, "the backup rewrote the database it was reading"
    assert _sha256(wal) == before_wal
    assert wal.stat().st_size == before_wal_size
    # Read-only must not have cost the uncheckpointed rows.
    assert manifest["row_counts"]["tasks"] == 4000


def test_inspect_database_does_not_checkpoint_a_hot_wal(tmp_path):
    """Same handle, same hazard: inspecting a database must not rewrite it."""
    src = tmp_path / "memory.db"
    wal = _hot_wal_db(src, rows=2000)
    before_db, before_wal = _sha256(src), _sha256(wal)

    report = inspect_database(str(src))

    assert report["row_counts"]["tasks"] == 2000
    assert wal.is_file()
    assert _sha256(src) == before_db
    assert _sha256(wal) == before_wal


def test_inspect_database_refuses_to_conjure_a_missing_file(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        inspect_database(str(missing))
    assert not missing.exists()


def test_rolling_generations_prune_oldest_only(tmp_path):
    from datetime import datetime, timedelta, timezone

    src = tmp_path / "memory.db"
    _make_db(src, rows=5).close()
    root = tmp_path / "b"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    made = [
        create_backup(
            str(src),
            backup_dir=str(root),
            generations=3,
            now=base + timedelta(hours=i),
        )["generation"]
        for i in range(5)
    ]

    kept = [os.path.basename(p) for p in list_backups(str(root))]
    assert kept == made[-3:]
    assert not (root / made[0]).exists()


def test_a_backdated_generation_never_prunes_itself(tmp_path):
    """An NTP step-back during a scheduled backup used to delete that backup.

    Retention sorts by name and the name is the timestamp, so a generation
    stamped earlier than everything already on disk sorted to the head of the
    prune list, removed itself, and still returned success — with
    ``pruned == [the generation just created]`` and a ``path`` that no longer
    existed. A silent no-op, reported as a good backup.
    """
    from datetime import datetime, timedelta, timezone

    src = tmp_path / "memory.db"
    _make_db(src, rows=5).close()
    root = tmp_path / "b"
    base = datetime(2026, 8, 2, tzinfo=timezone.utc)
    for i in range(3):
        create_backup(
            str(src), backup_dir=str(root), generations=3, now=base + timedelta(hours=i)
        )

    stepped_back = create_backup(
        str(src),
        backup_dir=str(root),
        generations=3,
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert stepped_back["generation"] == "20260801T000000Z"
    assert os.path.exists(stepped_back["path"]), "reported success, deleted the backup"
    assert os.path.exists(stepped_back["backup_path"])
    assert stepped_back["path"] not in stepped_back["pruned"]
    assert stepped_back["path"] in stepped_back["generations_kept"]
    assert verify_backup(stepped_back["path"])["ok"] is True
    # Retention is still bounded — it gave up the oldest of the *others*.
    assert [os.path.basename(p) for p in list_backups(str(root))] == [
        "20260801T000000Z",
        "20260802T010000Z",
        "20260802T020000Z",
    ]

    # Boundary: generations=1 still keeps the generation just written.
    lone = create_backup(
        str(src),
        backup_dir=str(root),
        generations=1,
        now=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    assert os.path.exists(lone["path"])
    assert [os.path.basename(p) for p in list_backups(str(root))] == ["20260731T000000Z"]


def test_retention_never_deletes_foreign_directories(tmp_path):
    """Regression guard: ~/.claude/memory/backups holds hand-made memory.db dirs."""
    src = tmp_path / "memory.db"
    _make_db(src, rows=5).close()
    root = tmp_path / "b"
    root.mkdir()

    foreign = root / "bridge_fix_20260519T164401Z"
    foreign.mkdir()
    (foreign / "memory.db").write_bytes(src.read_bytes())
    (foreign / "manifest.txt").write_text("hand written", encoding="utf-8")
    stamped_but_not_ours = root / "20260101T000000Z"
    stamped_but_not_ours.mkdir()
    (stamped_but_not_ours / "memory.db").write_bytes(b"x")

    for _ in range(4):
        create_backup(str(src), backup_dir=str(root), generations=1)

    assert (foreign / "memory.db").is_file()
    assert (foreign / "manifest.txt").is_file()
    assert (stamped_but_not_ours / "memory.db").is_file()
    assert len(list_backups(str(root))) == 1


def test_backup_refuses_missing_source_and_creates_nothing(tmp_path):
    root = tmp_path / "b"
    with pytest.raises(BackupError):
        create_backup(str(tmp_path / "nope.db"), backup_dir=str(root))
    assert not (tmp_path / "nope.db").exists()
    assert list_backups(str(root)) == []
    assert list(root.glob(".incoming-*")) == []


def test_backup_failure_leaves_no_partial_generation(tmp_path, monkeypatch):
    src = tmp_path / "memory.db"
    _make_db(src, rows=5).close()
    root = tmp_path / "b"

    def boom(*_a, **_kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db_utils, "_backup_to", boom)
    with pytest.raises(sqlite3.OperationalError):
        create_backup(str(src), backup_dir=str(root))

    assert list_backups(str(root)) == []
    assert list(root.glob(".incoming-*")) == []


def test_verify_backup_detects_tampering(tmp_path):
    src = tmp_path / "memory.db"
    _make_db(src, rows=20).close()
    manifest = create_backup(str(src), backup_dir=str(tmp_path / "b"))
    gen = tmp_path / "b" / manifest["generation"]

    conn = sqlite3.connect(str(gen / "memory.db"), isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("DELETE FROM tasks WHERE id < 5")
    conn.close()

    result = verify_backup(str(gen))
    assert result["ok"] is False
    assert any("sha256" in p for p in result["problems"])
    assert any("rows[tasks]" in p for p in result["problems"])


def test_inspect_database_survives_unreadable_virtual_tables(tmp_path):
    src = tmp_path / "memory.db"
    conn = _make_db(src, rows=3)
    # A virtual table whose module will not exist on a plain connection.
    conn.execute("CREATE TABLE ghost (a)")
    conn.close()
    # Rewrite the stored schema so 'ghost' claims an unavailable module.
    raw = sqlite3.connect(str(src), isolation_level=None)
    raw.execute("PRAGMA writable_schema=ON;")
    raw.execute(
        "UPDATE sqlite_master SET sql='CREATE VIRTUAL TABLE ghost USING vec0(a float[4])' "
        "WHERE name='ghost'"
    )
    raw.execute("PRAGMA writable_schema=OFF;")
    raw.close()

    report = inspect_database(str(src))
    assert report["row_counts"]["tasks"] == 3
    assert report["row_counts"]["ghost"] is None
    assert "ghost" in report["unreadable_tables"]


# ── Change 2: bounded logging ─────────────────────────────────────────────


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "memory").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("SQLITE_MEMORY_LOG_PER_PROCESS", raising=False)
    monkeypatch.delenv("SQLITE_MEMORY_LOG_DIR", raising=False)
    return home


def _fresh_logger(name: str, **kw) -> logging.Logger:
    logging.getLogger(name).handlers.clear()
    return setup_logger(name, **kw)


def test_setup_logger_installs_a_rotating_handler(isolated_home):
    logger = _fresh_logger("dur.rotating.1", log_file="server.log")
    try:
        (handler,) = logger.handlers
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes > 0
        assert handler.backupCount >= 1
        # Guard: the test must never have touched the operator's real log.
        assert str(isolated_home) in handler.baseFilename
    finally:
        for h in logger.handlers:
            h.close()
        logger.handlers.clear()


def test_rotation_bounds_are_read_at_call_time_not_import_time(isolated_home, monkeypatch):
    """``SQLITE_MEMORY_LOG_MAX_BYTES`` used to be unsettable by anything.

    It was read into a module constant at import, so no caller that had already
    imported db_utils — the test suite included — could influence it. The old
    assertion here, ``handler.maxBytes == db_utils.LOG_MAX_BYTES``, was a
    tautology: both sides came from that one read, so it held for every possible
    value and could not fail. Two different values set *after* import, each
    observed on the handler, can only pass if the read happens per call.
    """
    seen = []
    for index, (limit, keep) in enumerate(((4096, 3), (9001, 2))):
        monkeypatch.setenv("SQLITE_MEMORY_LOG_MAX_BYTES", str(limit))
        monkeypatch.setenv("SQLITE_MEMORY_LOG_BACKUP_COUNT", str(keep))
        logger = _fresh_logger(f"dur.rotating.env{index}", log_file="server.log")
        try:
            (handler,) = logger.handlers
            seen.append((handler.maxBytes, handler.backupCount))
        finally:
            for h in logger.handlers:
                h.close()
            logger.handlers.clear()

    assert seen == [(4096, 3), (9001, 2)]


def test_two_processes_never_share_one_rotating_sink(isolated_home, monkeypatch):
    """The multi-process rollover race, reduced to its cause: a shared filename.

    ``RotatingFileHandler`` rotation is not multi-process safe. When one of the
    seven MCP servers rolls over it renames ``server.log`` to ``server.log.1``
    while the others keep writing through descriptors that now point at the
    renamed inode; the next rollover renames a fresh ``server.log`` over the top
    and those records are gone. Distinct filenames remove the shared inode.
    """
    monkeypatch.setattr(db_utils.os, "getpid", lambda: 111)
    first = _fresh_logger("dur.rotating.pidA", log_file="server.log")
    monkeypatch.setattr(db_utils.os, "getpid", lambda: 222)
    second = _fresh_logger("dur.rotating.pidB", log_file="server.log")
    try:
        assert first.handlers[0].baseFilename.endswith("server.111.log")
        assert second.handlers[0].baseFilename.endswith("server.222.log")
        assert first.handlers[0].baseFilename != second.handlers[0].baseFilename
    finally:
        for logger in (first, second):
            for h in logger.handlers:
                h.close()
            logger.handlers.clear()


def test_log_growth_is_bounded(isolated_home):
    """The old FileHandler grew server.log to 110 MB beside the database."""
    logger = _fresh_logger(
        "dur.rotating.2", log_file="server.log", max_bytes=2048, backup_count=2
    )
    try:
        for i in range(4000):
            logger.warning("noisy line %05d %s", i, "x" * 60)
    finally:
        for h in logger.handlers:
            h.close()
        logger.handlers.clear()

    log_dir = isolated_home / ".claude" / "memory"
    stem = db_utils._log_file_name("server.log")
    files = sorted(log_dir.glob(f"{stem}*"))
    total = sum(f.stat().st_size for f in files)

    assert [f.name for f in files] == [stem, f"{stem}.1", f"{stem}.2"]
    assert not (log_dir / "server.log").exists(), "wrote to the shared multi-process sink"
    assert total <= 2048 * 3 * 1.1, f"unbounded: {total} bytes in {files}"
    # Unrotated, 4000 x ~90 byte records would be well over 300 KB.
    assert total < 100_000


def test_backup_count_is_never_zero(isolated_home):
    """backupCount=0 makes RotatingFileHandler re-open in append mode, i.e. never bound."""
    logger = _fresh_logger("dur.rotating.3", max_bytes=1024, backup_count=0)
    try:
        assert logger.handlers[0].backupCount == 1
    finally:
        for h in logger.handlers:
            h.close()
        logger.handlers.clear()


def test_per_process_log_files_are_the_default_and_opt_out_is_explicit(
    isolated_home, monkeypatch
):
    """The safe default is the opposite of the one originally chosen.

    Seven concurrent writer processes plus a rotating handler is a data-loss
    race, so a shared sink must be asked for, not inherited.
    """
    pid = os.getpid()
    assert db_utils._log_file_name("server.log") == f"server.{pid}.log"
    assert db_utils._log_file_name("noext") == f"noext.{pid}"

    for falsy in ("0", "false", "no", "off"):
        monkeypatch.setenv("SQLITE_MEMORY_LOG_PER_PROCESS", falsy)
        assert db_utils._log_file_name("server.log") == "server.log"

    for truthy in ("1", "true", "yes", "on"):
        monkeypatch.setenv("SQLITE_MEMORY_LOG_PER_PROCESS", truthy)
        assert db_utils._log_file_name("server.log") == f"server.{pid}.log"

    # Blank is "unset", not "off".
    monkeypatch.setenv("SQLITE_MEMORY_LOG_PER_PROCESS", "  ")
    assert db_utils._log_file_name("server.log") == f"server.{pid}.log"


def test_setup_logger_still_falls_back_to_tempdir(isolated_home, tmp_path, monkeypatch):
    """Mock-free: the primary candidate is a directory, so opening it must fail.

    tests/test_server_imports.py asserts this by patching ``logging.FileHandler``,
    which a RotatingFileHandler no longer routes through in a patchable way. The
    behaviour itself is unchanged, and this proves it without mocking the handler.
    """
    name = "fallback-probe.log"
    resolved = db_utils._log_file_name(name)
    (isolated_home / ".claude" / "memory" / resolved).mkdir()
    fake_tmp = tmp_path / "tmp"
    fake_tmp.mkdir()
    monkeypatch.setattr(db_utils.tempfile, "gettempdir", lambda: str(fake_tmp))

    logger = _fresh_logger("dur.rotating.fallback", log_file=name)
    try:
        assert logger.handlers
        assert isinstance(logger.handlers[0], logging.handlers.RotatingFileHandler)
        assert logger.handlers[0].baseFilename == str(
            fake_tmp / "sqlite-memory-mcp" / resolved
        )
    finally:
        for h in logger.handlers:
            h.close()
        logger.handlers.clear()


def test_log_dir_override_keeps_runs_out_of_the_live_directory(
    isolated_home, tmp_path, monkeypatch
):
    elsewhere = tmp_path / "logs"
    monkeypatch.setenv("SQLITE_MEMORY_LOG_DIR", str(elsewhere))
    resolved = db_utils._log_file_name("server.log")

    logger = _fresh_logger("dur.rotating.override", log_file="server.log")
    try:
        assert logger.handlers[0].baseFilename == str(elsewhere / resolved)
        assert not (isolated_home / ".claude" / "memory" / resolved).exists()
    finally:
        for h in logger.handlers:
            h.close()
        logger.handlers.clear()


def test_setup_logger_is_still_idempotent(isolated_home):
    logger = _fresh_logger("dur.rotating.4")
    try:
        assert setup_logger("dur.rotating.4") is logger
        assert len(logger.handlers) == 1
    finally:
        for h in logger.handlers:
            h.close()
        logger.handlers.clear()


# ── Change 3: document-frequency stopwords ────────────────────────────────

# Bulgarian function words that pass the len>=3 filter and are invisible to the
# English STOPWORDS list.
BG_FILLERS = ["това", "който", "може", "като", "след", "все", "защото", "така"]
BG_CONTENT = [
    "клавиатура",
    "паметта",
    "интерфейс",
    "заявка",
    "сървър",
    "индекс",
    "миграция",
    "протокол",
    "агент",
    "тест",
]


def _bilingual_corpus(n: int = 400, seed: int = 1729) -> list[str]:
    """Documents that are unrelated in content but share their function words.

    A wide content vocabulary matters: with a narrow one every pair overlaps on
    content and the measurement stops being about stopwords at all.
    """
    rng = random.Random(seed)
    vocab = BG_CONTENT + [f"термин{i:03d}" for i in range(400)]
    docs = []
    for _ in range(n):
        filler = rng.sample(BG_FILLERS, 6)
        content = rng.sample(vocab, 8)
        docs.append(" ".join(filler + content + ["the", "and", "with"]))
    return docs


@pytest.fixture(autouse=True)
def _reset_stopwords():
    yield
    set_similarity_stopwords(None)


def test_df_cutoff_removes_high_frequency_terms_in_any_language():
    learned = build_df_stopwords(_bilingual_corpus())

    assert set(BG_FILLERS) <= learned, "Bulgarian fillers survived the DF cutoff"
    assert {"the", "and", "with"} <= learned
    assert not (set(BG_CONTENT) & learned), "content words were wrongly dropped"


def test_df_cutoff_respects_the_ratio():
    # 'rare' is in 1 of 100 documents, 'common' in all of them.
    docs = ["common alpha beta"] * 99 + ["common rare gamma"]
    learned = build_df_stopwords(docs, min_documents=10)
    assert "common" in learned
    assert "rare" not in learned

    assert build_df_stopwords(docs, max_df_ratio=1.0, min_documents=10) == frozenset()
    with pytest.raises(ValueError):
        build_df_stopwords(docs, max_df_ratio=0.0)


def test_small_corpus_returns_nothing_and_keeps_the_bootstrap():
    docs = ["common alpha", "common beta"]
    assert build_df_stopwords(docs) == frozenset()

    stats = learn_similarity_stopwords(docs)
    assert stats["installed"] is False
    assert db_utils.active_similarity_stopwords() is BOOTSTRAP_STOPWORDS


def test_learned_set_cuts_random_pair_jaccard():
    """The measured failure: unrelated pairs crossing minimum_jaccard=0.15."""
    corpus = _bilingual_corpus(250)

    def p99(stopwords):
        tokens = [tokenize_for_similarity(doc, stopwords=stopwords) for doc in corpus]
        scores = []
        for i, left in enumerate(tokens):
            for right in tokens[i + 1 :]:
                union = left | right
                scores.append(len(left & right) / len(union) if union else 0.0)
        scores.sort()
        return scores[int(0.99 * (len(scores) - 1))]

    before = p99(BOOTSTRAP_STOPWORDS)
    after = p99(build_df_stopwords(corpus))

    assert before > 0.15, f"corpus does not reproduce the inflated floor ({before})"
    assert after < before
    assert after <= 0.15, f"DF cutoff left p99 above threshold: {after}"


def test_learn_from_a_temp_database_installs_the_set(tmp_path):
    db = tmp_path / "corpus.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE tasks (title TEXT, description TEXT, notes TEXT)")
    conn.execute("CREATE TABLE observations (content TEXT)")
    conn.executemany(
        "INSERT INTO tasks(title, description, notes) VALUES (?, '', '')",
        [(doc,) for doc in _bilingual_corpus(200)],
    )
    conn.executemany(
        "INSERT INTO observations(content) VALUES (?)",
        [(doc,) for doc in _bilingual_corpus(200, seed=7)],
    )
    conn.close()

    stats = learn_similarity_stopwords(db_path=str(db))

    assert stats["installed"] is True
    assert stats["documents"] == 400
    assert set(BG_FILLERS) <= db_utils.active_similarity_stopwords()
    assert "това" not in tokenize_for_similarity("това е клавиатура")
    assert tokenize_for_similarity("това е клавиатура") == {"клавиатура"}
    assert db_utils.similarity_stopword_stats()["stopwords"] == stats["stopwords"]


def test_learn_tolerates_a_database_without_the_corpus_tables(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    stats = learn_similarity_stopwords(db_path=str(db))
    assert stats["documents"] == 0
    assert stats["installed"] is False

    with pytest.raises(FileNotFoundError):
        learn_similarity_stopwords(db_path=str(tmp_path / "missing.db"))


def test_tokenize_keeps_its_old_contract():
    set_similarity_stopwords(None)
    assert tokenize_for_similarity("") == set()
    assert tokenize_for_similarity("The Quick brown FOX") == {"quick", "brown", "fox"}
    assert tokenize_for_similarity("a of on to") == set()
    assert db_utils.STOPWORDS is BOOTSTRAP_STOPWORDS
