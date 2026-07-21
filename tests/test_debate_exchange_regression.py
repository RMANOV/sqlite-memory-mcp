from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import os
import sqlite3
import subprocess
import sys

import pytest

from debate import (
    DebateError,
    bind_role_session,
    claim_worker_session,
    debate_post_with_recipients,
    init_debate,
    prepare_wake_dry_run,
    recover_stale_worker_claims,
    transition_state,
)
from schema import init_db


ROOT = Path(__file__).resolve().parent.parent


def _load_hook(name: str, relative: str):
    path = ROOT / relative
    spec = spec_from_file_location(name, path)
    if spec is None:
        loader = SourceFileLoader(name, str(path))
        spec = spec_from_file_location(name, path, loader=loader)
    module = module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def exchange_db(tmp_path):
    path = tmp_path / "memory.db"
    init_db(str(path))
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    init_debate(
        con,
        topic_id="EXCHANGE1",
        title="exchange regression",
        roles=[
            {"role": "CONDUCTOR", "session_id": "cc-conductor1"},
            {"role": "EXECUTOR", "session_id": "codex-executor1"},
        ],
        created_by_role="CONDUCTOR",
    )
    transition_state(con, topic_id="EXCHANGE1", role="CONDUCTOR", new_state="ACTIVE")
    bind_role_session(
        con,
        topic_id="EXCHANGE1",
        role="EXECUTOR",
        session_id="codex-executor1",
        reason="regression fixture",
    )
    con.commit()
    try:
        yield con, path
    finally:
        con.close()


def _trigger(con: sqlite3.Connection, body: str):
    return debate_post_with_recipients(
        con,
        topic_id="EXCHANGE1",
        role="CONDUCTOR",
        priority="H",
        kind="Q",
        body=body,
        addressed_to=["EXECUTOR"],
        vehicle="analysis",
    )


def _claim(con: sqlite3.Connection, trigger: dict):
    return claim_worker_session(
        con,
        topic_id="EXCHANGE1",
        role="EXECUTOR",
        parent_session_id="codex-executor1",
        trigger_msg_id=trigger["msg_id"],
    )


def test_wake_prompt_uses_real_worker_no_action_argument_and_bounded_codex_mode(
    monkeypatch,
):
    wake = _load_hook("debate_wake_exchange_regression", "hooks/debate_wake.py")
    # The codex route is gated OFF on Windows unless explicitly enabled
    # (advocate BLOCK high-risk #3: auto-spawned --dangerously-bypass is an
    # attack surface). This test verifies the command SHAPE, so opt in.
    monkeypatch.setenv("DEBATE_WAKE_CODEX_ENABLED", "1")
    prompt = wake._wake_prompt(
        {"target_role": "EXECUTOR", "target_session_id": "codex-executor1-W7"},
        "a00000000001",
        "EXCHANGE1",
    )
    command = wake._agent_command(
        {"target_runtime": "codex"}, "a00000000001", "EXCHANGE1"
    )
    assert "worker_session_id (the session_id shown above)" in prompt
    assert "topic_id, role, session_id, trigger_msg_id" not in prompt
    assert "--ephemeral" in command
    assert 'model_reasoning_effort="low"' in command
    source = (ROOT / "hooks/debate_wake.py").read_text(encoding="utf-8")
    assert '"CODEX_DEBATE_WRAPPER_BYPASS": "1"' in source


def test_implementation_refusal_audit_is_singleton_across_rescans(exchange_db):
    con, _path = exchange_db
    trigger = debate_post_with_recipients(
        con,
        topic_id="EXCHANGE1",
        role="CONDUCTOR",
        priority="H",
        kind="Q",
        body="implementation handoff",
        addressed_to=["EXECUTOR"],
        vehicle="implementation",
    )
    first = prepare_wake_dry_run(
        con, tool_response=trigger, action="post_tool_use_wake"
    )
    second = prepare_wake_dry_run(
        con, tool_response=trigger, action="post_tool_use_wake"
    )
    count = con.execute(
        "SELECT count(*) FROM debate_wake_log "
        "WHERE trigger_msg_id=? AND result='implementation_requires_impl_vehicle'",
        (trigger["msg_id"],),
    ).fetchone()[0]
    assert count == 1
    assert first["logs"][0]["duplicate"] is False
    assert second["logs"][0]["duplicate"] is True


def test_dead_worker_recovery_preserves_messages_and_parent_pending(exchange_db):
    con, _path = exchange_db
    terminal_trigger = _trigger(con, "terminal worker")
    orphan_trigger = _trigger(con, "orphan worker")
    live_trigger = _trigger(con, "live worker")
    terminal_claim = _claim(con, terminal_trigger)
    orphan_claim = _claim(con, orphan_trigger)
    live_claim = _claim(con, live_trigger)
    debate_post_with_recipients(
        con,
        topic_id="EXCHANGE1",
        role="EXECUTOR",
        priority="H",
        kind="A",
        body="terminal receipt",
        addressed_to=["CONDUCTOR"],
        reply_to=terminal_trigger["msg_id"],
        vehicle="analysis",
    )
    stale = (
        (datetime.now(timezone.utc) - timedelta(minutes=20))
        .isoformat()
        .replace("+00:00", "Z")
    )
    con.execute(
        "UPDATE debate_worker_claims SET heartbeat_at=? WHERE topic_id='EXCHANGE1'",
        (stale,),
    )
    before_messages = con.execute(
        "SELECT count(*) FROM debate_messages WHERE topic_id='EXCHANGE1'"
    ).fetchone()[0]
    cutoff = (
        (datetime.now(timezone.utc) - timedelta(minutes=15))
        .isoformat()
        .replace("+00:00", "Z")
    )
    out = recover_stale_worker_claims(
        con,
        topic_id="EXCHANGE1",
        older_than_ts=cutoff,
        minimum_age_seconds=120,
        live_worker_session_ids={live_claim["worker_session_id"]},
    )
    after_messages = con.execute(
        "SELECT count(*) FROM debate_messages WHERE topic_id='EXCHANGE1'"
    ).fetchone()[0]
    states = {
        row["worker_session_id"]: row["state"]
        for row in con.execute(
            "SELECT worker_session_id,state FROM debate_worker_claims "
            "WHERE topic_id='EXCHANGE1'"
        )
    }
    assert before_messages == after_messages
    assert states[terminal_claim["worker_session_id"]] == "completed"
    assert states[orphan_claim["worker_session_id"]] == "retired"
    assert states[live_claim["worker_session_id"]] == "active"
    assert out["completed_count"] == 1
    assert out["retired_count"] == 1
    assert out["skipped_live_count"] == 1
    assert (
        con.execute(
            "SELECT count(*) FROM debate_signal_state "
            "WHERE session_id='codex-executor1' AND topic_id='EXCHANGE1'"
        ).fetchone()[0]
        == 0
    )


def test_worker_recovery_validation_uses_worker_diagnostic_namespace(exchange_db):
    con, _path = exchange_db
    cutoff = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with pytest.raises(DebateError) as exc_info:
        recover_stale_worker_claims(
            con,
            topic_id="EXCHANGE1",
            older_than_ts=cutoff,
            minimum_age_seconds=120,
        )
    assert exc_info.value.error_type == "worker_claim_recovery_cutoff_too_recent"
    assert "worker_claim_recovery_cutoff_too_recent" in str(exc_info.value)


def test_pump_reads_only_init_or_active_topics_and_stop_wait_is_interruptible(
    tmp_path, monkeypatch
):
    pump = _load_hook("debate_pump_exchange_regression", "hooks/debate_pump.py")
    path = tmp_path / "memory.db"
    init_db(str(path))
    con = sqlite3.connect(path)
    try:
        for topic, state in (("ACTIVE1", "ACTIVE"), ("ARCHIVE1", "ARCHIVED")):
            con.execute(
                "INSERT INTO debates "
                "(topic_id,title,state,created_at,created_by_role,roles_json) "
                "VALUES (?,?,?,'2026-07-19T08:00:00Z','CONDUCTOR','[]')",
                (topic, topic, state),
            )
            msg_id = "a00000000001" if state == "ACTIVE" else "b00000000002"
            con.execute(
                "INSERT INTO debate_messages VALUES (?,?, 'CONDUCTOR',"
                "'2026-07-19T08:01:00Z','H','Q',NULL,'analysis',NULL,?,"
                "'2026-07-19T08:01:00Z')",
                (msg_id, topic, topic),
            )
            con.execute(
                "INSERT INTO debate_message_recipients VALUES (?, 'EXECUTOR','normal')",
                (msg_id,),
            )
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr(pump, "DB_PATH", path)
    rows = pump._fetch_new("1970-01-01T00:00:00Z", "", [], ["Q"], 20)
    assert [row["topic_id"] for row in rows] == ["ACTIVE1"]
    pump.STOP = False
    pump.STOP_EVENT.clear()
    started = datetime.now(timezone.utc)
    pump._handle_signal(15, None)
    assert pump._wait_or_stop(10.0) is True
    assert (datetime.now(timezone.utc) - started).total_seconds() < 0.5


@pytest.mark.parametrize("operation", ["unlink", "replace", "open"])
def test_pump_log_filesystem_failures_fall_back_without_escaping(
    operation, tmp_path, monkeypatch, capsys
):
    pump = _load_hook(f"debate_pump_log_{operation}_regression", "hooks/debate_pump.py")
    pump.LOG_PATH = tmp_path / "pump.jsonl"
    pump.LOG_MAX_BYTES = 1
    pump.LOG_KEEP = 1
    pump.LOG_PATH.write_text("rotation required\n", encoding="utf-8")

    if operation == "unlink":
        archive = pump.LOG_PATH.with_name(f"{pump.LOG_PATH.name}.1")
        archive.write_text("old archive\n", encoding="utf-8")
        original = Path.unlink

        def fail_unlink(path, *args, **kwargs):
            if path == archive:
                raise OSError("forced unlink failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_unlink)
    elif operation == "replace":
        original = Path.replace

        def fail_replace(path, target):
            if path == pump.LOG_PATH:
                raise OSError("forced replace failure")
            return original(path, target)

        monkeypatch.setattr(Path, "replace", fail_replace)
    else:
        pump.LOG_MAX_BYTES = 1024 * 1024
        original = Path.open

        def fail_open(path, *args, **kwargs):
            if path == pump.LOG_PATH:
                raise OSError("forced open failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_open)

    pump._log("regression_probe", operation=operation)

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["event"] == "pump_log_fallback"
    assert payload["failed_event"] == "regression_probe"
    assert operation in payload["error"]


def test_regression_probe_is_clean():
    if os.environ.get("DEBATE_REGRESSION_SYNTHETIC_BROKEN") == "1":
        pytest.fail("synthetic debate exchange regression")
    probe = (ROOT / "tests/fixtures/debate_regression_probe.txt").read_text(
        encoding="utf-8"
    )
    assert probe.splitlines()[0] == "CLEAN"


def test_systemd_path_gate_watches_exchange_code_and_has_runtime_bound():
    path_unit = (ROOT / "systemd/user/sqlite-memory-debate-regression.path").read_text(
        encoding="utf-8"
    )
    service = (ROOT / "systemd/user/sqlite-memory-debate-regression.service").read_text(
        encoding="utf-8"
    )
    for relative in (
        "debate.py",
        "debate_retrieval.py",
        "debate_prompt_context.py",
        "intel_server.py",
        "schema.py",
        "hooks/debate_pump.py",
        "hooks/debate_wake.py",
        "tests/fixtures/debate_regression_probe.txt",
    ):
        assert relative in path_unit
    assert "codex-debate-wrapper" in path_unit
    assert "codex-debate-adaptive-watch" in path_unit
    assert "TimeoutStartSec=150" in service
    assert "--timeout-seconds 120" in service
    assert "memory.db" not in service


def test_installed_codex_wrapper_fails_closed_on_global_subscriptions():
    wrapper = Path.home() / ".local/bin/codex-debate-wrapper"
    if not wrapper.exists():
        pytest.skip("host Codex debate wrapper is not installed")
    source = wrapper.read_text(encoding="utf-8")
    assert "CODEX_DEBATE_LEGACY_SUBSCRIPTIONS" in source
    assert "rank_pending_from_memory_db" in source
    assert "CODEX_DEBATE_BODY_BYTES" in source
    assert "global subscriptions are opt-in" in source


def test_retrieval_and_intel_server_import_from_foreign_cwd(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import debate_retrieval, debate_prompt_context, intel_server",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_installed_watcher_prefers_newest_active_topic(tmp_path):
    watcher_path = Path.home() / ".local/bin/codex-debate-adaptive-watch"
    if not watcher_path.exists():
        pytest.skip("host adaptive watcher is not installed")
    watcher = _load_hook("adaptive_watch_exchange_regression", str(watcher_path))
    db_path = tmp_path / "watch.db"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(
            "CREATE TABLE debates(topic_id TEXT,state TEXT,created_at TEXT);"
            "CREATE TABLE debate_role_bindings("
            "topic_id TEXT,role TEXT,session_id TEXT,generation INT,"
            "state TEXT,runtime TEXT);"
        )
        for topic, created in (
            ("DAILY_OLD", "2026-07-04T00:00:00Z"),
            ("DAILY_NEW", "2026-07-19T00:00:00Z"),
        ):
            con.execute("INSERT INTO debates VALUES (?, 'ACTIVE', ?)", (topic, created))
            con.execute(
                "INSERT INTO debate_role_bindings VALUES "
                "(?, 'EXECUTOR', 'codex-executor1', 1, 'active', 'codex')",
                (topic,),
            )
        rows = watcher.active_bindings(con)
    finally:
        con.close()
    assert [row["topic_id"] for row in rows] == ["DAILY_NEW", "DAILY_OLD"]
