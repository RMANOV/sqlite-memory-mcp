"""BUILD STEP 1 acceptance + negative-isolation tests (read-only debate tabs).

Falsifiable checks for the S2 receipt:
  * fences: prod-path fail-closed guard; mode=ro + query_only blocks writes;
    harness opens fixtures ONLY (never prod).
  * frozen-clock reproduction under as_of=2026-07-18T18:42:27Z:
      T2 FX-A section_a == 0;  T1 FX-B recent(role='CODEX_FIXTURE') == 10 in
      ts DESC;  T3 FX-B section_a == 10;  T6 FX-B topics targets == 10;
      T4 FX-B board_search 15 nonces per-source exact-equality vs the board.
  * T7 read-only isolation: DebateListWidget + the debate: namespace guards can
    never reach a TaskDB write.

Run: pytest tests/test_tray_readonly_debate.py -q
"""
import datetime as _dt
import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

FIXDIR = "/home/rmanov/vawm-spec/fixtures"
FX_A = os.path.join(FIXDIR, "FX-A-zero-parity.db")
FX_B = os.path.join(FIXDIR, "FX-B-seeded.db")
PROD = os.path.expanduser("~/.claude/memory/memory.db")
BOARD_DIR = "/home/rmanov/sqlite-memory-mcp-worktrees/memory-board-nullfloor/operator_board"

AS_OF = _dt.datetime(2026, 7, 18, 18, 42, 27, tzinfo=_dt.timezone.utc)
REC_TARGETS = [f"fxb-rec-{i:02d}" for i in range(1, 11)]
WAIT_TARGETS = {f"fxb-wait-{i:02d}" for i in range(1, 11)}
TOPIC_TARGETS = {f"fxb-topic-{i:02d}" for i in range(1, 11)}
NONCES = {
    "debate": [f"fxbqzdebate{i:02d}" for i in range(1, 6)],
    "tasks": [f"fxbqztask{i:02d}" for i in range(1, 6)],
    "knowledge": [f"fxbqzknow{i:02d}" for i in range(1, 6)],
}


def asof_clock():
    return AS_OF


pytestmark = pytest.mark.skipif(
    not (os.path.exists(FX_A) and os.path.exists(FX_B)),
    reason="frozen fixtures not present",
)


# ── fences ────────────────────────────────────────────────────────────────
def test_prod_path_fail_closed():
    from debate_read_dao import DebateReadDAO

    with pytest.raises(PermissionError):
        DebateReadDAO(PROD, forbid_path=PROD)
    # a symlink / relative form resolving to prod is also refused
    with pytest.raises(PermissionError):
        DebateReadDAO(os.path.expanduser("~/.claude/memory/../memory/memory.db"),
                      forbid_path=PROD)


def test_query_only_blocks_writes():
    from debate_read_dao import DebateReadDAO
    import sqlite3

    dao = DebateReadDAO(FX_B, clock=asof_clock, forbid_path=PROD)
    try:
        with pytest.raises(sqlite3.OperationalError):
            dao._conn.execute("CREATE TABLE _should_fail(x)")
        with pytest.raises(sqlite3.OperationalError):
            dao._conn.execute(
                "INSERT INTO debate_messages(msg_id,topic_id,role,ts,priority,kind,body,created_at)"
                " VALUES('x','y','z','t','M','A','b','t')"
            )
    finally:
        dao.close()


# ── frozen-clock reproduction ───────────────────────────────────────────────
def test_T2_fxa_section_a_zero():
    from debate_read_dao import DebateReadDAO

    dao = DebateReadDAO(FX_A, clock=asof_clock, forbid_path=PROD)
    try:
        items, cand = dao.waiting_section_a()
        assert len(items) == 0, f"FX-A section_a must be 0 (zero-parity), got {len(items)}"
    finally:
        dao.close()


def test_T1_fxb_recent_role_pin_exactly_10_in_order():
    from debate_read_dao import DebateReadDAO

    dao = DebateReadDAO(FX_B, clock=asof_clock, forbid_path=PROD)
    try:
        pinned = dao.recent(1.0, "CODEX_FIXTURE", ["DECISION", "STATE", "STATUS"])
        ids = [x["msg_id"] for x in pinned["items"]]
        assert ids == REC_TARGETS, f"role-pinned recent must be the 10 targets in ts DESC, got {ids}"
        # role=None interleaves real prod rows → pin is required
        loose = dao.recent(1.0, None, ["DECISION", "STATE", "STATUS"])
        assert loose["count"] == 24
    finally:
        dao.close()


def test_T3_fxb_section_a_10():
    from debate_read_dao import DebateReadDAO

    dao = DebateReadDAO(FX_B, clock=asof_clock, forbid_path=PROD)
    try:
        items, _ = dao.waiting_section_a()
        assert {x["msg_id"] for x in items} == WAIT_TARGETS
        assert len(items) == 10
    finally:
        dao.close()


def test_T6_fxb_topics_10_targets():
    from debate_read_dao import DebateReadDAO

    dao = DebateReadDAO(FX_B, clock=asof_clock, forbid_path=PROD)
    try:
        tids = {t["topic_id"] for t in dao.topics()["topics"]}
        assert TOPIC_TARGETS <= tids
        thread = dao.topic_thread("fxb-topic-01")
        assert thread["count"] == 3  # a→b→c
    finally:
        dao.close()


def test_T4_fxb_search_nonces_hit_only_their_source():
    from debate_read_dao import DebateReadDAO

    dao = DebateReadDAO(FX_B, clock=asof_clock, forbid_path=PROD)
    try:
        for source, toks in NONCES.items():
            for tok in toks:
                res = dao.board_search(tok, 25)
                assert len(res[source]) >= 1, f"{tok} must hit {source}"
                for other in ("debate", "tasks", "knowledge"):
                    if other != source:
                        assert len(res[other]) == 0, f"{tok} leaked into {other}"
    finally:
        dao.close()


def test_T4_fxb_search_exact_equality_vs_board():
    """Per-source id list + order must equal the board reference on FX-B (M4)."""
    if not os.path.exists(os.path.join(BOARD_DIR, "board.py")):
        pytest.skip("board reference not available")
    from debate_read_dao import DebateReadDAO

    if BOARD_DIR not in sys.path:
        sys.path.insert(0, BOARD_DIR)
    import board as B

    class FrozenDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return AS_OF if tz is not None else AS_OF.replace(tzinfo=None)

    B.datetime = FrozenDT
    ref_board = B.Board(B.DB(FX_B))
    dao = DebateReadDAO(FX_B, clock=asof_clock, forbid_path=PROD)
    try:
        all_toks = NONCES["debate"] + NONCES["tasks"] + NONCES["knowledge"]
        for tok in all_toks:
            mine = dao.board_search(tok, 25)
            ref = ref_board.search(tok, 25)
            for src, idkey in (("debate", "msg_id"), ("tasks", "id"), ("knowledge", "id")):
                mine_ids = [r[idkey] for r in mine[src]]
                ref_ids = [r[idkey] for r in ref[src]]
                assert mine_ids == ref_ids, f"{tok}/{src}: {mine_ids} != board {ref_ids}"
    finally:
        dao.close()


def test_time_to_find_data_precondition():
    """Structural time-to-find precondition (spec T1/T3/T6): the target set is
    exactly the first screen (zero scroll) and the read is sub-100ms."""
    import time
    from debate_read_dao import DebateReadDAO

    dao = DebateReadDAO(FX_B, clock=asof_clock, forbid_path=PROD)
    try:
        for label, fn, expected in (
            ("recent", lambda: dao.recent(1.0, "CODEX_FIXTURE", ["DECISION", "STATE", "STATUS"])["items"], 10),
            ("waiting", lambda: dao.waiting_section_a()[0], 10),
            ("topics", lambda: [t for t in dao.topics()["topics"] if t["topic_id"].startswith("fxb-topic-")], 10),
        ):
            t0 = time.perf_counter()
            rows = fn()
            dt_ms = (time.perf_counter() - t0) * 1000
            assert len(rows) == expected, f"{label}: {len(rows)} != {expected}"
            assert dt_ms < 100, f"{label} read {dt_ms:.1f}ms > 100ms"
    finally:
        dao.close()


# ── T7 read-only isolation ──────────────────────────────────────────────────
@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _ast_reference_scan(path):
    """Return (imported_modules, called_or_attr_names) via AST — ignores
    docstrings/comments so documentation mentioning a banned name is fine."""
    import ast

    tree = ast.parse(open(path).read())
    imports, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return imports, names


def test_debate_widget_holds_no_db(qapp):
    from debate_list_widget import DebateListWidget

    w = DebateListWidget()
    assert not hasattr(w, "db"), "DebateListWidget must hold no db handle"
    imports, names = _ast_reference_scan(os.path.join(REPO, "debate_list_widget.py"))
    assert "db_utils" not in imports and "subprocess" not in imports
    for banned in ("apply_task_mutation", "update_task", "mark_done", "delete_task"):
        assert banned not in names, f"debate_list_widget must not call {banned!r}"


def test_debate_widget_double_click_is_navigation_only(qapp):
    from debate_list_widget import DebateListWidget

    w = DebateListWidget()
    w.add_debate_row("m1", "row one", topic_id="t1", copy_payload="blk")
    seen = []
    w.navigate_requested.connect(lambda s: seen.append(s))
    w._on_double_click(w.item(0))
    assert seen == ["t1"], "double-click must emit in-app navigation only"


def test_debate_widget_context_menu_readonly(qapp, monkeypatch):
    import debate_list_widget as dlw

    labels = []

    class FakeMenu:
        def __init__(self, *a, **k):
            pass

        def addAction(self, label):
            labels.append(label)
            return label

        def exec(self, *a, **k):
            return None  # user cancels — nothing selected

    monkeypatch.setattr(dlw, "QMenu", FakeMenu)
    w = dlw.DebateListWidget()
    w.add_debate_row("m1", "row one", topic_id="t1")
    from PyQt6.QtCore import QPoint

    w._context_menu(QPoint(0, 0))
    assert labels == ["Copy msg_id", "Copy row", "Open thread"], (
        f"context menu must be read-only, got {labels}"
    )
    assert "Delete" not in labels and "Convert to Note" not in labels


def test_tasklistwidget_debate_guard_double_click(qapp):
    """Defense-in-depth: a debate: row placed in a TaskListWidget never opens a
    reader or mutates (spec B3)."""
    from tray_dialogs import TaskListWidget
    from PyQt6.QtWidgets import QListWidgetItem
    from PyQt6.QtCore import Qt

    db = MagicMock()
    w = TaskListWidget(db)
    w._open_reader = MagicMock()
    item = QListWidgetItem("debate row")
    item.setData(Qt.ItemDataRole.UserRole, "debate:m1")
    w.addItem(item)
    w._on_double_click(item)
    w._open_reader.assert_not_called()
    for meth in ("update_task", "mark_done", "delete_task"):
        assert not getattr(db, meth).called


def test_tasklistwidget_debate_guard_context_menu(qapp, monkeypatch):
    from tray_dialogs import TaskListWidget
    import PyQt6.QtWidgets as QtW
    from PyQt6.QtWidgets import QListWidgetItem
    from PyQt6.QtCore import Qt, QPoint

    db = MagicMock()
    w = TaskListWidget(db)
    item = QListWidgetItem("debate row")
    item.setData(Qt.ItemDataRole.UserRole, "debate:m1")
    w.addItem(item)

    built = {"menu": False}

    class FakeMenu:
        def __init__(self, *a, **k):
            built["menu"] = True

        def setStyleSheet(self, *a):
            pass

        def addAction(self, *a):
            return object()

        def exec(self, *a, **k):
            return None

    # _context_menu does `from PyQt6.QtWidgets import QMenu` locally → patch the
    # source module so a broken guard (which would build the task menu) is caught.
    monkeypatch.setattr(QtW, "QMenu", FakeMenu)
    w.itemAt = lambda pos: item
    w._context_menu(QPoint(0, 0))
    assert built["menu"] is False, "debate: row must not build the task context menu"
    assert not db.delete_task.called


def test_on_item_changed_debate_guard(qapp, monkeypatch):
    """FullWindow._on_item_changed early-returns for debate: rows (no scheduled
    mutation)."""
    import task_tray
    from PyQt6.QtCore import Qt

    calls = []
    monkeypatch.setattr(task_tray, "QTimer",
                        type("T", (), {"singleShot": staticmethod(lambda *a, **k: calls.append(a))}))

    class FakeItem:
        def __init__(self, key):
            self._k = key

        def data(self, role):
            return self._k

        def checkState(self):
            return Qt.CheckState.Unchecked

    fake_self = MagicMock()
    # debate: row → guard returns before scheduling
    task_tray.FullWindow._on_item_changed(fake_self, FakeItem("debate:m1"))
    assert calls == [], "debate: row must not schedule a mutation"
    # control: a real task id DOES schedule (guard is specific)
    task_tray.FullWindow._on_item_changed(fake_self, FakeItem("real-task-uuid"))
    assert len(calls) == 1, "non-debate id must schedule normally"
