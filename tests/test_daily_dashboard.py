import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import (
    bridge_change_summary,
    dash_today,
    dash_topic_id,
    dash_upsert,
    ensure_dashboard_schema,
    export_index_json,
    export_task_files,
    get_daily_dashboard,
    now_iso,
)
from schema import init_db


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _conn(db_path: Path):
    init_db(str(db_path))
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    ensure_dashboard_schema(conn)
    return conn


def _insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    title: str,
    *,
    section: str = "today",
    updated_at: str | None = None,
) -> None:
    ts = updated_at or now_iso()
    conn.execute(
        "INSERT INTO tasks "
        "(id, title, description, status, priority, section, type, created_at, updated_at) "
        "VALUES (?, ?, '', 'not_started', 'medium', ?, 'task', ?, ?)",
        (task_id, title, section, ts, ts),
    )


def _seed_daily_topic(
    conn: sqlite3.Connection, *, session_id: str = "cc-conductor"
) -> str:
    topic_id = dash_topic_id()
    ts = now_iso()
    conn.execute(
        "INSERT INTO debates "
        "(topic_id, title, state, created_at, created_by_role, roles_json, metadata_json) "
        "VALUES (?, 'Daily', 'INIT', ?, 'CONDUCTOR', ?, NULL)",
        (
            topic_id,
            ts,
            json.dumps([{"role": "CONDUCTOR", "session_id": session_id}]),
        ),
    )
    conn.execute(
        "INSERT INTO debate_role_bindings "
        "(topic_id, role, session_id, runtime, state, generation, created_at, updated_at, "
        "retired_at, reason, bound_by_role, bound_by_msg_id) "
        "VALUES (?, 'CONDUCTOR', ?, 'cc', 'active', 1, ?, ?, NULL, 'test', NULL, NULL)",
        (topic_id, session_id, ts, ts),
    )
    return topic_id


def test_dashboard_schema_enum_slot_and_body_guards(tmp_path):
    conn = _conn(tmp_path / "memory.db")
    _insert_task(conn, "task-alpha", "Alpha")

    out = dash_upsert(
        conn,
        task_id="task-alpha",
        kind="decision",
        slot="main",
        body="ship",
        allow_test_override=True,
    )

    assert out["kind"] == "decision"
    with pytest.raises(ValueError, match="kind"):
        dash_upsert(
            conn,
            task_id="task-alpha",
            kind="noise",
            slot="main",
            body="bad",
            allow_test_override=True,
        )
    with pytest.raises(ValueError, match="slot"):
        dash_upsert(
            conn,
            task_id="task-alpha",
            kind="decision",
            slot="",
            body="bad",
            allow_test_override=True,
        )
    with pytest.raises(ValueError, match="240"):
        dash_upsert(
            conn,
            task_id="task-alpha",
            kind="decision",
            slot="long",
            body="x" * 241,
            allow_test_override=True,
        )


def test_dashboard_replace_and_distinct_slots_survive(tmp_path):
    conn = _conn(tmp_path / "memory.db")
    _insert_task(conn, "task-alpha", "Alpha")

    dash_upsert(
        conn,
        task_id="task-alpha",
        kind="result",
        slot="a",
        body="old",
        allow_test_override=True,
    )
    dash_upsert(
        conn,
        task_id="task-alpha",
        kind="result",
        slot="a",
        body="new",
        allow_test_override=True,
    )
    dash_upsert(
        conn,
        task_id="task-alpha",
        kind="result",
        slot="b",
        body="second",
        allow_test_override=True,
    )

    rows = get_daily_dashboard(conn)
    assert [(r["slot"], r["body"]) for r in rows] == [("b", "second"), ("a", "new")]


def test_dashboard_day_scoped_read(tmp_path):
    conn = _conn(tmp_path / "memory.db")
    _insert_task(conn, "task-alpha", "Alpha")

    dash_upsert(
        conn,
        task_id="task-alpha",
        kind="decision",
        slot="today",
        body="today body",
        day="2026-06-07",
        allow_test_override=True,
    )
    dash_upsert(
        conn,
        task_id="task-alpha",
        kind="decision",
        slot="tomorrow",
        body="tomorrow body",
        day="2026-06-08",
        allow_test_override=True,
    )

    assert [r["body"] for r in get_daily_dashboard(conn, day="2026-06-07")] == [
        "today body"
    ]
    assert [r["body"] for r in get_daily_dashboard(conn, day="2026-06-08")] == [
        "tomorrow body"
    ]


def test_dashboard_hard_conductor_guard(tmp_path):
    conn = _conn(tmp_path / "memory.db")
    _insert_task(conn, "task-alpha", "Alpha")
    _seed_daily_topic(conn, session_id="cc-conductor-live")

    with pytest.raises(PermissionError, match="not active CONDUCTOR"):
        dash_upsert(
            conn,
            task_id="task-alpha",
            kind="decision",
            slot="main",
            body="blocked",
            writer_session="codex-executor20260602",
        )

    dash_upsert(
        conn,
        task_id="task-alpha",
        kind="decision",
        slot="main",
        body="allowed",
        writer_session="cc-conductor-live",
    )
    assert get_daily_dashboard(conn)[0]["body"] == "allowed"


def test_dashboard_caps_per_kind_and_per_task(tmp_path):
    conn = _conn(tmp_path / "memory.db")
    _insert_task(conn, "task-alpha", "Alpha")

    for i in range(9):
        dash_upsert(
            conn,
            task_id="task-alpha",
            kind="result",
            slot=f"r{i}",
            body=f"result {i}",
            updated_at=f"2026-06-07T00:00:{i:02d}+00:00",
            allow_test_override=True,
        )
    result_rows = conn.execute(
        "SELECT slot FROM daily_dashboard WHERE task_id='task-alpha' AND kind='result' "
        "ORDER BY slot"
    ).fetchall()
    assert "r0" not in [r["slot"] for r in result_rows]
    assert len(result_rows) == 8

    kinds = ["decision", "difficulty", "misunderstanding", "advice", "option", "result"]
    for k_idx, kind in enumerate(kinds):
        for i in range(8):
            dash_upsert(
                conn,
                task_id="task-alpha",
                kind=kind,
                slot=f"{kind}-{i}",
                body=f"{kind} {i}",
                updated_at=f"2026-06-07T00:{k_idx:02d}:{i:02d}+00:00",
                allow_test_override=True,
            )
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM daily_dashboard WHERE task_id='task-alpha'"
    ).fetchone()["c"]
    assert total == 40


def test_dashboard_bridge_exclusion(tmp_path):
    conn = _conn(tmp_path / "memory.db")
    _insert_task(conn, "task-alpha", "Alpha", updated_at="2026-06-07T00:00:00+00:00")

    dash_upsert(
        conn,
        task_id="task-alpha",
        kind="decision",
        slot="main",
        body="dashboard only",
        updated_at="2026-06-07T00:00:10+00:00",
        allow_test_override=True,
    )

    summary = bridge_change_summary(conn, "2026-06-07T00:00:05+00:00")
    assert summary["changed_tasks"] == 0

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    export_task_files(conn, str(bridge_dir))
    export_index_json(conn, str(bridge_dir))
    assert not (bridge_dir / "daily_dashboard.json").exists()
    assert "daily_dashboard" not in (bridge_dir / "index.json").read_text()


def test_bin_task_dash_set_dash_and_rm_use_temp_db(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = _conn(db_path)
    _insert_task(conn, "task-alpha", "Alpha")
    conn.close()
    home = tmp_path / "home"
    (home / ".claude" / "memory").mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "TASK_DB": str(db_path),
            "SQLITE_MEMORY_DASH_TEST_OVERRIDE": "1",
        }
    )

    script = Path(__file__).resolve().parents[1] / "bin" / "task"
    set_run = subprocess.run(
        [
            str(script),
            "dash-set",
            "task-alp",
            "decision",
            "main",
            "ship",
            "now",
            "--priority",
            "H",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "dashboard set" in set_run.stdout

    dash_run = subprocess.run(
        [str(script), "dash"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "-- Alpha --" in dash_run.stdout
    assert "[D] ship now (H)" in dash_run.stdout

    rm_run = subprocess.run(
        [str(script), "dash-rm", "task-alp", "decision", "main"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "dashboard removed: 1" in rm_run.stdout


def test_bin_task_fb_self_heals_roles_and_posts_to_conductor(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = _conn(db_path)
    topic_id = dash_topic_id()
    conn.execute(
        "INSERT INTO debates "
        "(topic_id, title, state, created_at, created_by_role, roles_json, metadata_json) "
        "VALUES (?, 'Daily', 'INIT', ?, 'CONDUCTOR', '[]', NULL)",
        (topic_id, now_iso()),
    )
    conn.close()
    home = tmp_path / "home"
    (home / ".claude" / "memory").mkdir(parents=True)
    env = os.environ.copy()
    env.update({"HOME": str(home), "TASK_DB": str(db_path)})

    script = Path(__file__).resolve().parents[1] / "bin" / "task"
    run = subprocess.run(
        [str(script), "fb", "task-alp", "operator says adjust"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "sent to conductor:" in run.stdout

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    roles = {
        (r["role"], r["session_id"])
        for r in conn.execute(
            "SELECT role, session_id FROM debate_role_bindings WHERE topic_id=?",
            (topic_id,),
        )
    }
    assert ("HUMAN", "human-rmanov") in roles
    assert ("CONDUCTOR", "cc-conductor") in roles
    msg = conn.execute(
        "SELECT m.role, m.kind, m.body, r.recipient "
        "FROM debate_messages m JOIN debate_message_recipients r ON r.msg_id=m.msg_id"
    ).fetchone()
    assert msg["role"] == "HUMAN"
    assert msg["kind"] == "Q"
    assert msg["recipient"] == "CONDUCTOR"
    assert "operator says adjust" in msg["body"]


def test_dashboard_qt_renderer_uses_no_item_flags_and_blocks_load_signals(qapp):
    import task_tray
    from PyQt6.QtCore import Qt
    from tray_dialogs import TaskListWidget

    class _Db:
        db_path = ":memory:"

    widget = TaskListWidget(_Db())
    calls = []
    original = widget.blockSignals

    def spy(value):
        calls.append(value)
        return original(value)

    widget.blockSignals = spy
    window = task_tray.FullWindow.__new__(task_tray.FullWindow)
    task_tray.FullWindow._load_dashboard_tab(
        window,
        widget,
        [
            {
                "day": dash_today(),
                "task_id": "task-alpha",
                "kind": "decision",
                "slot": "main",
                "body": "ship",
                "priority": "H",
                "updated_at": now_iso(),
                "task_title": "Alpha",
                "task_section": "today",
            },
            {
                "day": dash_today(),
                "task_id": "orphan-1",
                "kind": "difficulty",
                "slot": "main",
                "body": "blocked",
                "priority": "M",
                "updated_at": now_iso(),
                "task_title": None,
                "task_section": None,
            },
        ],
    )

    assert calls[0] is True
    assert calls[-1] is False
    assert widget.count() == 4
    assert "Other / debate work-items" in widget.item(2).text()
    for i in range(widget.count()):
        assert widget.item(i).flags() == Qt.ItemFlag.NoItemFlags


def test_empty_dashboard_hidden_and_today_is_start_tab(qapp, tmp_path):
    import task_tray
    from PyQt6.QtCore import QSettings

    settings = QSettings("TaskTray", "FullWindow")
    old_active_tab = settings.value("active_tab")
    old_active_tab_key = settings.value("active_tab_key")
    settings.setValue("active_tab", 0)
    settings.setValue("active_tab_key", "dashboard")

    db = task_tray.TaskDB(str(tmp_path / "memory.db"))
    db.add_task("Open daily task", section="today")
    window = None
    try:
        window = task_tray.FullWindow(db)
        window.refresh()

        assert len(window._raw_cache.get("dashboard", [])) == 0
        assert window.tabs.isTabVisible(window._tab_keys.index("dashboard")) is False
        assert window._tab_keys[window.tabs.currentIndex()] == "today"
        assert len(window._raw_cache.get("today", [])) == 1
    finally:
        if window is not None:
            window.deleteLater()
        db.close()
        if old_active_tab is None:
            settings.remove("active_tab")
        else:
            settings.setValue("active_tab", old_active_tab)
        if old_active_tab_key is None:
            settings.remove("active_tab_key")
        else:
            settings.setValue("active_tab_key", old_active_tab_key)
