"""Fresh-topic consumers must follow current C3 bindings, never retired roles."""

import importlib.util
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import db_utils
import debate
import intel_server
from schema import init_db

DAY = "2026-11-14"
TOPIC = db_utils.dash_topic_id(DAY)
ROLE = "ADVOCATE_CODEX"
SESSION = "codex-currentreviewer"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    path = tmp_path / "reviewer.db"
    init_db(str(path))
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    roles = [{"role": ROLE, "session_id": SESSION}]
    debate.init_debate(
        conn,
        topic_id=TOPIC,
        title="New C3 daily topic",
        roles=roles,
        created_by_role=ROLE,
    )
    debate.seed_initial_role_bindings(
        conn, topic_id=TOPIC, roles=roles, bound_by_role=ROLE
    )
    monkeypatch.delenv("SQLITE_MEMORY_DASH_TEST_OVERRIDE", raising=False)
    monkeypatch.setattr(
        intel_server,
        "_get_conn_immediate",
        lambda: db_utils.get_conn_immediate(str(path)),
    )
    yield conn, path
    conn.close()


def _dashboard(conn, session=SESSION):
    return db_utils.dash_upsert(
        conn,
        day=DAY,
        writer_session=session,
        task_id="task1",
        kind="advice",
        slot="next",
        body="Review the task",
    )


def _priority(conn, session=SESSION, role=ROLE):
    # On the merged pre-fix code this reaches the real retired-role condition;
    # after adding attribution, the identical test uses that required argument.
    args = {"topic_id": TOPIC, "role": role, "lane": "P1", "reason": "Current review"}
    if "author_session_id" in inspect.signature(debate.set_topic_priority).parameters:
        args["author_session_id"] = session
    return debate.set_topic_priority(conn, **args)


def test_new_reviewer_can_write_and_retract_dashboard(fresh):
    conn, _ = fresh
    _dashboard(conn)
    row = conn.execute("SELECT body, author FROM daily_dashboard").fetchone()
    assert tuple(row) == ("Review the task", ROLE)
    assert (
        db_utils.dash_retract(
            conn,
            day=DAY,
            writer_session=SESSION,
            task_id="task1",
            kind="advice",
            slot="next",
        )
        == 1
    )


def test_new_reviewer_can_set_topic_priority(fresh):
    conn, _ = fresh
    assert _priority(conn)["lane"] == "P1"
    metadata = json.loads(
        conn.execute("SELECT metadata_json FROM debates").fetchone()[0]
    )
    assert metadata["conductor_priority"]["updated_by_role"] == ROLE
    assert metadata["conductor_priority"]["author_session_id"] == SESSION


@pytest.mark.parametrize("surface", ["dashboard", "priority"])
@pytest.mark.parametrize(
    "condition",
    [
        "missing",
        "wrong",
        "retired",
        "diagnostic",
        "ambiguous",
        "unbacked_pin",
        "cross_topic",
    ],
)
def test_current_reviewer_rejects_unresolved_identity_without_mutation(
    fresh, surface, condition
):
    conn, _ = fresh
    session = SESSION
    if condition in {"missing", "wrong"}:
        session = "" if condition == "missing" else "codex-wrongreviewer"
    elif condition in {"retired", "diagnostic"}:
        conn.execute("UPDATE debate_role_bindings SET state=?", (condition,))
    elif condition == "ambiguous":
        debate.add_role_to_debate(
            conn,
            topic_id=TOPIC,
            role="ADVOCATE",
            session_id="cc-secondreviewer",
            reason="second candidate",
        )
    elif condition == "cross_topic":
        conn.execute("UPDATE debate_role_bindings SET session_id='codex-othercurrent'")
        debate.init_debate(
            conn,
            topic_id="ANOTHER_DAY",
            title="Another topic",
            roles=[{"role": ROLE, "session_id": SESSION}],
            created_by_role=ROLE,
        )
        debate.seed_initial_role_bindings(
            conn,
            topic_id="ANOTHER_DAY",
            roles=[{"role": ROLE, "session_id": SESSION}],
            bound_by_role=ROLE,
        )
    else:
        conn.execute(
            "UPDATE debates SET metadata_json=?",
            (
                json.dumps(
                    {
                        "governance": {
                            "mode": "authority",
                            "authority_role": ROLE,
                            "authority_session_id": SESSION,
                            "authority_generation": 1,
                            "authority_epoch": 1,
                        }
                    }
                ),
            ),
        )
    db_utils.ensure_dashboard_schema(conn)
    before = {
        name: [tuple(r) for r in conn.execute(f"SELECT * FROM {name}")]
        for name in ("debates", "debate_role_bindings", "daily_dashboard")
    }
    with pytest.raises((PermissionError, debate.DebateError)):
        (_dashboard if surface == "dashboard" else _priority)(conn, session)
    assert before == {
        name: [tuple(r) for r in conn.execute(f"SELECT * FROM {name}")]
        for name in before
    }


@pytest.mark.parametrize("surface", ["dashboard", "priority"])
def test_historical_conductor_remains_readable_but_cannot_write(fresh, surface):
    conn, _ = fresh
    conn.execute("DELETE FROM debate_role_bindings")
    roles = [{"role": "CONDUCTOR", "session_id": SESSION}]
    conn.execute(
        "UPDATE debates SET roles_json=?, metadata_json=NULL", (json.dumps(roles),)
    )
    conn.execute(
        "INSERT INTO debate_role_bindings (topic_id,role,session_id,runtime,state,"
        "generation,created_at,updated_at,reason) VALUES (?, 'CONDUCTOR', ?, 'codex',"
        "'active', 1, '2026-01-01', '2026-01-01', 'historical fixture')",
        (TOPIC, SESSION),
    )
    before = tuple(conn.execute("SELECT * FROM debates").fetchone())
    assert debate.get_debate(conn, TOPIC)["roles"] == roles
    with pytest.raises((PermissionError, debate.DebateError)):
        if surface == "dashboard":
            _dashboard(conn)
        else:
            _priority(conn, role="CONDUCTOR")
    assert tuple(conn.execute("SELECT * FROM debates").fetchone()) == before


def test_priority_mcp_exposes_and_enforces_exact_current_session(fresh):
    _, _path = fresh
    result = json.loads(
        intel_server.debate_set_topic_priority(
            topic_id=TOPIC,
            role=ROLE,
            lane="P2",
            reason="native wrapper shape",
            author_session_id=SESSION,
        )
    )
    assert result.get("lane") == "P2", result
    denied = json.loads(
        intel_server.debate_set_topic_priority(
            topic_id=TOPIC,
            role=ROLE,
            lane="P0",
            reason="wrong session",
            author_session_id="codex-foreign",
        )
    )
    assert "error_type" in denied


def test_priority_cli_uses_explicit_reviewer_and_session(fresh):
    conn, path = fresh
    env = dict(os.environ, SQLITE_MEMORY_DB=str(path))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "debate_ops",
            "set-priority",
            TOPIC,
            "P3",
            "operator review",
            "--role",
            ROLE,
            "--author-session-id",
            SESSION,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["lane"] == "P3"
    assert (
        json.loads(conn.execute("SELECT metadata_json FROM debates").fetchone()[0])[
            "conductor_priority"
        ]["updated_by_role"]
        == ROLE
    )


def test_wake_prompt_and_receipt_do_not_invent_retired_recipient(fresh, monkeypatch):
    conn, path = fresh
    debate.transition_state(
        conn, topic_id=TOPIC, role=ROLE, new_state="ACTIVE", author_session_id=SESSION
    )
    message = debate.post_message(
        conn,
        topic_id=TOPIC,
        role=ROLE,
        priority="M",
        kind="Q",
        body="Review this task",
        author_session_id=SESSION,
    )
    monkeypatch.setenv("SQLITE_MEMORY_DB", str(path))
    source = Path(__file__).resolve().parents[1] / "hooks" / "debate_wake.py"
    spec = importlib.util.spec_from_file_location("current_reviewer_wake", source)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    target = {"target_role": "EXECUTOR_1", "target_session_id": "cc-worker"}
    receipt = hook._receipt_event(target, message["msg_id"], TOPIC)
    assert receipt["write_to"] == ROLE
    assert "CONDUCTOR" not in hook._wake_prompt(target, message["msg_id"], TOPIC)


def test_wake_receipt_does_not_borrow_sender_from_another_topic(fresh, monkeypatch):
    conn, path = fresh
    debate.transition_state(
        conn, topic_id=TOPIC, role=ROLE, new_state="ACTIVE", author_session_id=SESSION
    )
    message = debate.post_message(
        conn,
        topic_id=TOPIC,
        role=ROLE,
        priority="M",
        kind="Q",
        body="Review this task",
        author_session_id=SESSION,
    )
    monkeypatch.setenv("SQLITE_MEMORY_DB", str(path))
    source = Path(__file__).resolve().parents[1] / "hooks" / "debate_wake.py"
    spec = importlib.util.spec_from_file_location("foreign_reviewer_wake", source)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    receipt = hook._receipt_event(
        {"target_role": "EXECUTOR_1"}, message["msg_id"], "FOREIGN_TOPIC"
    )
    assert receipt["from_role"] == "UNKNOWN"
    assert receipt["write_to"] != ROLE
