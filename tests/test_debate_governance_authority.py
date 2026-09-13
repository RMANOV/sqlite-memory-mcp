"""C3 A1 roster contract through the real public wrappers.

The retired-role case is intentional RED until the production gate is added.
All data and session identifiers below are synthetic; only temporary DBs are used.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import intel_server  # noqa: E402
from schema import init_db  # noqa: E402

EXECUTOR_SESSION = "codex-c3a1exec01"
RECIPIENT_SESSION = "codex-c3a1exec02"
SNAPSHOT_TABLES = (
    "debates",
    "debate_role_bindings",
    "debate_messages",
    "debate_message_recipients",
    "debate_worker_claims",
)


@pytest.fixture
def governance_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "governance.db")
    init_db(db_path)

    @contextlib.contextmanager
    def factory():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    monkeypatch.setattr(intel_server, "_get_conn", factory)
    monkeypatch.setattr(intel_server, "_get_conn_immediate", factory)
    monkeypatch.setattr(
        intel_server, "_signal_wake_after_commit", lambda: None, raising=False
    )
    for name in (
        "SQLITE_MEMORY_DEBATE_GATE_ENABLED",
        "SQLITE_MEMORY_DEBATE_GATE_DISABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    return db_path


def _snapshot(db_path):
    """Read every row of the fixed allowlist; recipient rows need no topic_id."""
    conn = sqlite3.connect(db_path)
    try:
        return {
            table: sorted(
                (tuple(row) for row in conn.execute(f"SELECT * FROM {table}")),
                key=repr,
            )
            for table in SNAPSHOT_TABLES
        }
    finally:
        conn.close()


def _init(topic_id, roles):
    return json.loads(
        intel_server.debate_init(
            topic_id=topic_id,
            title="synthetic roster contract",
            roles_json=json.dumps(roles),
            created_by_role="EXECUTOR_1",
            metadata_json=json.dumps(
                {"priority_lane": "P2", "priority_reason": "synthetic roster test"}
            ),
        )
    )


def test_new_roster_rejects_retired_conductor_without_rows(governance_db):
    before = _snapshot(governance_db)
    assert all(not rows for rows in before.values()), before
    out = _init(
        "C3A1BAD",
        [
            {"role": "EXECUTOR_1", "session_id": EXECUTOR_SESSION},
            {"role": "EXECUTOR_2", "session_id": RECIPIENT_SESSION},
            {"role": "CONDUCTOR", "session_id": "codex-c3a1old01"},
        ],
    )
    after = _snapshot(governance_db)
    assert out.get("error_type") == "role_retired", {
        "actual_outcome": out,
        "before": before,
        "after": after,
    }
    assert after == before


def test_numbered_executor_legacy_topic_stays_available(governance_db):
    roles = [
        {"role": "ADVOCATE_CODEX", "session_id": "codex-c3a1adv01"},
        {"role": "EXECUTOR_1", "session_id": EXECUTOR_SESSION},
        {"role": "EXECUTOR_2", "session_id": RECIPIENT_SESSION},
    ]
    created = _init("C3A1GOOD", roles)
    assert "error_type" not in created, created
    assert created["roles"] == roles

    conn = sqlite3.connect(governance_db)
    try:
        stored = conn.execute(
            "SELECT roles_json FROM debates WHERE topic_id = ?", ("C3A1GOOD",)
        ).fetchone()
        assert stored is not None
        assert json.loads(stored[0]) == roles
        bindings = conn.execute(
            "SELECT role, session_id, state FROM debate_role_bindings "
            "WHERE topic_id = ?",
            ("C3A1GOOD",),
        ).fetchall()
        assert set(bindings) == {
            (entry["role"], entry["session_id"], "active") for entry in roles
        }
    finally:
        conn.close()

    activated = json.loads(
        intel_server.debate_state(
            topic_id="C3A1GOOD",
            role="EXECUTOR_1",
            new_state="ACTIVE",
            reason="synthetic availability guard",
            author_session_id=EXECUTOR_SESSION,
        )
    )
    assert "error_type" not in activated, activated
    conn = sqlite3.connect(governance_db)
    try:
        assert conn.execute(
            "SELECT state FROM debates WHERE topic_id = ?", ("C3A1GOOD",)
        ).fetchone() == ("ACTIVE",)
    finally:
        conn.close()

    body = "synthetic ordinary legacy availability"
    posted = json.loads(
        intel_server.debate_post_with_recipients(
            topic_id="C3A1GOOD",
            role="EXECUTOR_1",
            author_session_id=EXECUTOR_SESSION,
            priority="M",
            kind="STATUS",
            body=body,
            addressed_to_csv="EXECUTOR_2",
        )
    )
    assert "msg_id" in posted, posted
    readback = json.loads(
        intel_server.debate_read(topic_id="C3A1GOOD", role="EXECUTOR_2")
    )
    assert "error_type" not in readback, readback
    messages = [
        row for row in readback["messages"] if row["msg_id"] == posted["msg_id"]
    ]
    assert len(messages) == 1, readback
    assert messages[0]["body"] == body
    assert posted["author_session_id"] == EXECUTOR_SESSION
    # Legacy read projects identity/body; stored provenance is a writer contract.
    conn = sqlite3.connect(f"file:{governance_db}?mode=ro", uri=True)
    try:
        assert conn.execute(
            "SELECT author_session_id FROM debate_messages WHERE msg_id = ?",
            (posted["msg_id"],),
        ).fetchone() == (EXECUTOR_SESSION,)
    finally:
        conn.close()


# Synthetic pre-migration state is seeded explicitly below. It must not depend
# on public creation of a CONDUCTOR roster remaining permitted after retirement.
LEGACY_TOPIC = "C3A1HISTORY"
LEGACY_SESSION = "codex-c3a1history01"
LEGACY_MSG = "c3a100000001"
LEGACY_BODY = "c3legacybeacon historical conductor evidence"
LEGACY_TS = "2026-01-01T00:00:00Z"
MATRIX_EXTRA_TABLES = (
    "debate_delivery_queue",
    "debate_messages_fts",
    "debate_watermarks",
    "debate_signal_state",
    "debate_signal_deliveries",
    "debate_wake_log",
    "debate_worker_counters",
)


def _matrix_snapshot(db_path):
    rows = _snapshot(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows.update(
            {
                table: sorted(
                    (tuple(row) for row in conn.execute(f"SELECT * FROM {table}")),
                    key=repr,
                )
                for table in MATRIX_EXTRA_TABLES
            }
        )
    finally:
        conn.close()
    return rows


def _seed_legacy_topic(db_path, conductor_state="retired"):
    """Represent existing historical rows, never an authorization bypass."""
    roles = [
        {"role": "EXECUTOR_1", "session_id": EXECUTOR_SESSION},
        {"role": "EXECUTOR_2", "session_id": RECIPIENT_SESSION},
        {"role": "CONDUCTOR", "session_id": LEGACY_SESSION},
    ]
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO debates "
                "(topic_id, title, state, created_at, created_by_role, roles_json, "
                "metadata_json) VALUES (?, ?, 'ACTIVE', ?, 'EXECUTOR_1', ?, ?)",
                (
                    LEGACY_TOPIC, "synthetic pre-migration topic", LEGACY_TS,
                    json.dumps(roles),
                    json.dumps({"priority_lane": "P2", "priority_reason": "fixture"}),
                ),
            )
            for entry in roles:
                state = conductor_state if entry["role"] == "CONDUCTOR" else "active"
                conn.execute(
                    "INSERT INTO debate_role_bindings "
                    "(topic_id, role, session_id, runtime, state, generation, "
                    "created_at, updated_at, retired_at, reason) "
                    "VALUES (?, ?, ?, 'codex', ?, 1, ?, ?, ?, ?)",
                    (
                        LEGACY_TOPIC, entry["role"], entry["session_id"], state,
                        LEGACY_TS, LEGACY_TS,
                        LEGACY_TS if state == "retired" else None,
                        "synthetic historical binding",
                    ),
                )
            # Omitted provenance columns deliberately retain genuine legacy
            # defaults. No parent attribution, grant, or capability is fabricated.
            conn.execute(
                "INSERT INTO debate_messages "
                "(msg_id, topic_id, role, ts, priority, kind, body, created_at) "
                "VALUES (?, ?, 'CONDUCTOR', ?, 'M', 'STATUS', ?, ?)",
                (LEGACY_MSG, LEGACY_TOPIC, LEGACY_TS, LEGACY_BODY, LEGACY_TS),
            )
            conn.execute(
                "INSERT INTO debate_message_recipients (msg_id, recipient) "
                "VALUES (?, 'EXECUTOR_1')",
                (LEGACY_MSG,),
            )
    finally:
        conn.close()
    return LEGACY_TOPIC


def _ordinary_topic():
    topic = "C3A1ORDINARY"
    out = _init(
        topic,
        [
            {"role": "EXECUTOR_1", "session_id": EXECUTOR_SESSION},
            {"role": "EXECUTOR_2", "session_id": RECIPIENT_SESSION},
        ],
    )
    assert out.get("state") == "INIT", out
    activated = json.loads(
        intel_server.debate_state(
            topic_id=topic, role="EXECUTOR_1", new_state="ACTIVE",
            reason="synthetic ordinary availability", author_session_id=EXECUTOR_SESSION,
        )
    )
    assert "error_type" not in activated, activated
    return topic


def _assert_matrix_refusal(db_path, before, out, error_type):
    after = _matrix_snapshot(db_path)
    assert (out.get("error_type"), after == before) == (error_type, True), {
        "expected_error": error_type,
        "actual_outcome": out,
        "changed_tables": [table for table in before if before[table] != after[table]],
    }


@pytest.mark.parametrize("history", ["new", "retired", "active"])
def test_add_role_rejects_conductor_including_legacy_reassert(governance_db, history):
    topic = (
        _ordinary_topic()
        if history == "new"
        else _seed_legacy_topic(governance_db, history)
    )
    before = _matrix_snapshot(governance_db)
    out = json.loads(
        intel_server.debate_add_role(
            topic_id=topic, role="CONDUCTOR", session_id=LEGACY_SESSION,
            reason="synthetic retired role reassertion",
        )
    )
    _assert_matrix_refusal(governance_db, before, out, "role_retired")


@pytest.mark.parametrize("state", ["active", "diagnostic"])
@pytest.mark.parametrize("session", [LEGACY_SESSION, "codex-c3a1newold"])
def test_binding_rejects_new_and_reactivated_conductor(governance_db, state, session):
    topic = _seed_legacy_topic(governance_db)
    before = _matrix_snapshot(governance_db)
    out = json.loads(
        intel_server.debate_bind_role(
            topic_id=topic, role="CONDUCTOR", session_id=session, state=state,
            reason="synthetic forbidden binding",
        )
    )
    _assert_matrix_refusal(governance_db, before, out, "role_retired")


def _post_through_dao(db_path, kwargs):
    # Exercise the real DAO in a real transaction, with its real author resolver.
    from debate import DebateError, post_message

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            out = post_message(conn, **kwargs)
        except DebateError as exc:
            conn.execute("ROLLBACK")
            return {"error_type": exc.error_type, "error": str(exc)}
        conn.execute("COMMIT")
        return out
    finally:
        conn.close()


@pytest.mark.parametrize("surface", ["plain_wrapper", "addressed_wrapper", "dao"])
def test_new_conductor_authorship_rejected_even_with_historical_active_owner(
    governance_db, surface,
):
    topic = _seed_legacy_topic(governance_db, "active")
    before = _matrix_snapshot(governance_db)
    kwargs = dict(
        topic_id=topic, role="CONDUCTOR", author_session_id=LEGACY_SESSION,
        priority="M", kind="STATUS", body="synthetic new retired-role post",
    )
    if surface == "dao":
        out = _post_through_dao(governance_db, kwargs)
    elif surface == "addressed_wrapper":
        out = json.loads(
            intel_server.debate_post_with_recipients(
                **kwargs, addressed_to_csv="EXECUTOR_2",
            )
        )
    else:
        out = json.loads(intel_server.debate_post(**kwargs))
    _assert_matrix_refusal(governance_db, before, out, "author_role_retired")


@pytest.mark.parametrize("recipients", ["CONDUCTOR", "EXECUTOR_2,CONDUCTOR"])
def test_new_normal_conductor_recipients_rejected_atomically(governance_db, recipients):
    topic = _seed_legacy_topic(governance_db)
    before = _matrix_snapshot(governance_db)
    out = json.loads(
        intel_server.debate_post_with_recipients(
            topic_id=topic, role="EXECUTOR_1", author_session_id=EXECUTOR_SESSION,
            priority="M", kind="STATUS", body="synthetic forbidden recipient",
            addressed_to_csv=recipients,
        )
    )
    _assert_matrix_refusal(governance_db, before, out, "recipient_role_retired")


@pytest.mark.parametrize("normal_recipients", ["", "EXECUTOR_2"])
def test_new_diagnostic_conductor_recipient_resolves_retired_role(
    governance_db, normal_recipients,
):
    topic = _seed_legacy_topic(governance_db, "diagnostic")
    before = _matrix_snapshot(governance_db)
    out = json.loads(
        intel_server.debate_post_with_recipients(
            topic_id=topic, role="EXECUTOR_1", author_session_id=EXECUTOR_SESSION,
            priority="M", kind="STATUS", body="synthetic forbidden diagnostic target",
            addressed_to_csv=normal_recipients, diagnostic_to_csv=LEGACY_SESSION,
        )
    )
    _assert_matrix_refusal(governance_db, before, out, "recipient_role_retired")


@pytest.mark.parametrize("binding_state", ["active", "diagnostic", "retired"])
def test_historical_conductor_read_and_fts_remain_available(governance_db, binding_state):
    topic = _seed_legacy_topic(governance_db, binding_state)
    before = _matrix_snapshot(governance_db)
    readback = json.loads(intel_server.debate_read(topic_id=topic, role="EXECUTOR_1"))
    assert "error_type" not in readback, readback
    messages = [row for row in readback["messages"] if row["msg_id"] == LEGACY_MSG]
    assert len(messages) == 1, readback
    assert (messages[0]["role"], messages[0]["body"]) == ("CONDUCTOR", LEGACY_BODY)
    literal = json.loads(
        intel_server.debate_search(
            topic_id=topic, query="c3legacybeacon", viewer_role="EXECUTOR_1",
        )
    )
    assert [row["msg_id"] for row in literal["messages"]] == [LEGACY_MSG], literal
    ranked = json.loads(
        intel_server.debate_context_search(
            topic_id=topic, query="c3legacybeacon", role="EXECUTOR_1",
            session_id=EXECUTOR_SESSION,
        )
    )
    assert "error_type" not in ranked, ranked
    hits = [row for row in ranked["results"] if row["msg_id"] == LEGACY_MSG]
    assert len(hits) == 1, ranked
    assert ranked["paths"]["fts_bm25"] == 1, ranked
    assert hits[0]["source_ranks"]["fts_bm25"] == 1, hits
    conn = sqlite3.connect(f"file:{governance_db}?mode=ro", uri=True)
    try:
        assert conn.execute(
            "SELECT author_session_id, provenance_class FROM debate_messages "
            "WHERE msg_id = ?", (LEGACY_MSG,),
        ).fetchone() == (None, "legacy")
    finally:
        conn.close()
    assert _matrix_snapshot(governance_db) == before


def test_already_retired_historical_row_can_stay_retired_without_uncovering(governance_db):
    topic = _seed_legacy_topic(governance_db)
    before = _matrix_snapshot(governance_db)
    out = json.loads(
        intel_server.debate_bind_role(
            topic_id=topic, role="CONDUCTOR", session_id=LEGACY_SESSION,
            state="retired", reason="synthetic retained retirement",
        )
    )
    assert "error_type" not in out, out
    assert out["state"] == "retired", out
    assert out["ownership_gap_override"] is False, out
    after = _matrix_snapshot(governance_db)
    for table in before:
        if table != "debate_role_bindings":
            assert after[table] == before[table], table
    old_bindings = before["debate_role_bindings"]
    new_bindings = after["debate_role_bindings"]
    assert len(old_bindings) == len(new_bindings) == 3
    old_target = [row for row in old_bindings if row[2] == LEGACY_SESSION][0]
    new_target = [row for row in new_bindings if row[2] == LEGACY_SESSION][0]
    assert [row for row in old_bindings if row[2] != LEGACY_SESSION] == [
        row for row in new_bindings if row[2] != LEGACY_SESSION
    ]
    # Only updated_at / retired_at / reason may be refreshed on that exact row.
    assert tuple(v for n, v in enumerate(old_target) if n not in (7, 8, 9)) == tuple(
        v for n, v in enumerate(new_target) if n not in (7, 8, 9)
    )
    assert new_target[4] == "retired"


def test_ordinary_added_role_and_diagnostic_delivery_remain_available(governance_db):
    topic = _ordinary_topic()
    added = json.loads(
        intel_server.debate_add_role(
            topic_id=topic, role="EXECUTOR_3", session_id="codex-c3a1exec03",
            reason="synthetic numbered lane",
        )
    )
    assert added.get("added_role") is True, added
    assert added["state"] == "active", added
    diagnostic_session = "codex-c3a1diag02"
    bound = json.loads(
        intel_server.debate_bind_role(
            topic_id=topic, role="EXECUTOR_2", session_id=diagnostic_session,
            state="diagnostic", reason="synthetic ordinary diagnostic lane",
        )
    )
    assert bound.get("state") == "diagnostic", bound
    assert bound["ownership_gap_override"] is False, bound
    posted = json.loads(
        intel_server.debate_post_with_recipients(
            topic_id=topic, role="EXECUTOR_1", author_session_id=EXECUTOR_SESSION,
            priority="M", kind="STATUS", body="synthetic valid mixed delivery",
            addressed_to_csv="EXECUTOR_3", diagnostic_to_csv=diagnostic_session,
        )
    )
    assert "msg_id" in posted, posted
    assert posted["recipient_count"] == 2, posted
    assert posted["diagnostic_recipient_count"] == 1, posted
    conn = sqlite3.connect(f"file:{governance_db}?mode=ro", uri=True)
    try:
        assert conn.execute(
            "SELECT recipient, recipient_mode FROM debate_message_recipients "
            "WHERE msg_id = ? ORDER BY recipient", (posted["msg_id"],),
        ).fetchall() == [
            ("EXECUTOR_3", "normal"), (diagnostic_session, "diagnostic"),
        ]
        assert conn.execute(
            "SELECT recipient FROM debate_delivery_queue "
            "WHERE msg_id = ? ORDER BY recipient", (posted["msg_id"],),
        ).fetchall() == [("EXECUTOR_3",), (diagnostic_session,)]
        assert conn.execute(
            "SELECT author_session_id, provenance_class FROM debate_messages "
            "WHERE msg_id = ?", (posted["msg_id"],),
        ).fetchone() == (EXECUTOR_SESSION, "parent")
        assert conn.execute(
            "SELECT state FROM debate_role_bindings "
            "WHERE topic_id = ? AND role = 'EXECUTOR_2' AND session_id = ?",
            (topic, RECIPIENT_SESSION),
        ).fetchone() == ("active",)
    finally:
        conn.close()
