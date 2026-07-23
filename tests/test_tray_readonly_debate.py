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


requires_frozen_fixtures = pytest.mark.skipif(
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


def test_S2a_refusal_precedes_any_db_open(monkeypatch):
    """Fail-closed refusal happens BEFORE any sqlite3.connect — so prod is never
    opened and no prod -wal/-shm is touched (S2a)."""
    import debate_read_dao as drd

    calls = []
    real_connect = drd.sqlite3.connect

    def spy_connect(*a, **k):
        calls.append(a[0] if a else k.get("database"))
        return real_connect(*a, **k)

    monkeypatch.setattr(drd.sqlite3, "connect", spy_connect)
    with pytest.raises(PermissionError) as ei:
        drd.DebateReadDAO(PROD, forbid_path=PROD)
    assert calls == [], f"prod must never be opened; sqlite3.connect calls={calls}"
    assert "refused" in str(ei.value).lower()
    # no prod sidecar files were created by us
    for ext in ("-wal", "-shm"):
        # (they may exist from live actors; we only assert WE did not connect)
        pass


@requires_frozen_fixtures
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
@requires_frozen_fixtures
def test_T2_fxa_section_a_zero():
    """Layer-1 board parity: FX-A section-A (live_await=False) == 0 (unchanged by
    adoption fix 1)."""
    from debate_read_dao import DebateReadDAO

    dao = DebateReadDAO(FX_A, clock=asof_clock, forbid_path=PROD)
    try:
        items, cand = dao.waiting_section_a(live_await=False)
        assert len(items) == 0, f"FX-A layer-1 must be 0 (board parity), got {len(items)}"
    finally:
        dao.close()


@requires_frozen_fixtures
def test_LIVE_AWAIT_fxa_drops_broad_keyword_false_positives():
    """The frozen prod snapshot contains historical operator keywords but no
    explicit unresolved operator request. The live view must stay empty rather
    than reviving those receipts as work."""
    from debate_read_dao import DebateReadDAO

    dao = DebateReadDAO(FX_A, clock=asof_clock, forbid_path=PROD)
    try:
        layer1, _ = dao.waiting_section_a(live_await=False)
        combined, _ = dao.waiting_section_a(live_await=True)
        assert len(layer1) == 0
        assert combined == [], "historical operator keywords are not pending asks"
    finally:
        dao.close()


@requires_frozen_fixtures
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


@requires_frozen_fixtures
def test_T3_fxb_section_a_10():
    """Layer-1 (human- recipient) parity: FX-B section-A (live_await=False) is
    exactly the 10 seeded targets. The live combined view is a superset."""
    from debate_read_dao import DebateReadDAO

    dao = DebateReadDAO(FX_B, clock=asof_clock, forbid_path=PROD)
    try:
        layer1, _ = dao.waiting_section_a(live_await=False)
        assert {x["msg_id"] for x in layer1} == WAIT_TARGETS
        assert len(layer1) == 10
        combined, _ = dao.waiting_section_a(live_await=True)
        assert WAIT_TARGETS <= {x["msg_id"] for x in combined}  # 10 targets still present
        assert len(combined) >= 10
    finally:
        dao.close()


def _build_synth_debate_db(path, now):
    """Minimal debate DB (no FK) for the layer-2 unit test."""
    import sqlite3

    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE debates(topic_id TEXT PRIMARY KEY, title TEXT, state TEXT,"
        " created_at TEXT, created_by_role TEXT, roles_json TEXT);"
        "CREATE TABLE debate_messages(msg_id TEXT PRIMARY KEY, topic_id TEXT, role TEXT,"
        " ts TEXT, priority TEXT, kind TEXT, reply_to TEXT, body TEXT, created_at TEXT);"
        "CREATE TABLE debate_message_recipients(msg_id TEXT, recipient TEXT,"
        " recipient_mode TEXT DEFAULT 'normal', PRIMARY KEY(msg_id,recipient));"
    )

    def ts(days=0, hours=0):
        from datetime import timedelta
        return (now + timedelta(days=days, hours=hours)).isoformat()

    rows = [
        # m1: role-addressed marker → layer 2 surfaces
        ("m1", "t", "CONDUCTOR", ts(-2), "H", "Q", None, "чака операторска ръка за deploy решение", ts(-2)),
        # m2: marker but already-given (own body _A_TAKEN) → excluded
        ("m2", "t", "ADVOCATE", ts(-1), "M", "DECISION", None, "операторско GO записано, продължаваме", ts(-1)),
        # m3: generic 'кажи' is addressed to a team role, not mechanically to
        # the operator → excluded (this broad marker caused live false positives)
        ("m3", "t", "EXECUTOR3", ts(-3), "M", "STATUS", None, "кажи дали да пусна сега", ts(-3)),
        # m4: human- recipient → layer 1 (must be deduped from layer 2)
        ("m4", "t", "CONDUCTOR", ts(-2, -1), "H", "Q", None, "чака операторска ръка", ts(-2, -1)),
        # m5: marker but >21d → excluded
        ("m5", "t", "CONDUCTOR", ts(-30), "H", "Q", None, "чака операторска ръка", ts(-30)),
        # m6 + m6r: resolved (descendant records decision taken) → excluded
        ("m6", "t", "ADVOCATE", ts(-4), "M", "DECISION", None, "нужна операторска ръка", ts(-4)),
        ("m6r", "t", "CONDUCTOR", ts(-3, -12), "M", "STATUS", "m6", "операторско решение взето", ts(-3, -12)),
        # m7: exact production regression. A role-routed implementation order
        # merely states a deployment guardrail; it does NOT wait on the operator.
        ("m7", "t", "CONDUCTOR", ts(-6), "H", "Q", None,
         "EXECUTOR — НОВО РАЗПОРЕЖДАНЕ. Без нов push/merge/deploy без ADVOCATE gate и операторски GO.", ts(-6)),
        # m8 + m8r: an explicit ask later superseded by a final completed stack.
        ("m8", "t", "CONDUCTOR", ts(-1), "M", "STATUS", None,
         "ЧАКАТ операторска sudo команда", ts(-1)),
        ("m8r", "t", "CONDUCTOR", ts(-1, 1), "M", "STATUS", "m8",
         "КОРЕКЦИЯ. ФИНАЛЕН ИНСТАЛИРАН СТЕК — всичко работи.", ts(-1, 1)),
        # m9: audit/receipt prose may quote the classifier vocabulary later in
        # the body. Only the leading paragraph can declare an actionable ask.
        ("m9", "t", "EXECUTOR", ts(0, -2), "M", "STATUS", None,
         "HOTFIX RECEIPT: strict classifier installed.\n\n"
         "Implementation detail: accepts explicit pending operator language.", ts(0, -2)),
    ]
    c.executemany("INSERT INTO debate_messages VALUES(?,?,?,?,?,?,?,?,?)", rows)
    c.execute("INSERT INTO debate_message_recipients(msg_id,recipient) VALUES('m4','human-operator')")
    c.execute("INSERT INTO debates VALUES('t','T','ACTIVE',?,?,'[]')", (ts(-5), "CONDUCTOR"))
    c.commit()
    c.close()


def test_ADOPTIONFIX1_live_await_surfaces_role_addressed(tmp_path):
    """Layer 2 surfaces explicit body-marker asks; excludes broad historical /
    guardrail keywords, already-given, aged-out, and resolved rows; dedups the
    human-recipient row against layer 1."""
    import datetime as _dt
    from debate_read_dao import DebateReadDAO

    now = _dt.datetime(2026, 7, 18, 12, 0, 0, tzinfo=_dt.timezone.utc)
    db = str(tmp_path / "synth.db")
    _build_synth_debate_db(db, now)
    dao = DebateReadDAO(db, clock=lambda: now, forbid_path=PROD)
    try:
        layer1 = {x["msg_id"] for x in dao.waiting_section_a(live_await=False)[0]}
        combined = {x["msg_id"] for x in dao.waiting_section_a(live_await=True)[0]}
        assert layer1 == {"m4"}, f"layer 1 (human- recipient) should be m4, got {layer1}"
        assert combined == {"m1", "m4"}, f"combined mismatch: {combined}"
        assert "m2" not in combined  # already-given GO
        assert "m3" not in combined  # generic team-role 'кажи', not an operator ask
        assert "m5" not in combined  # >21d
        assert "m6" not in combined  # resolved in-thread
        assert "m7" not in combined  # implementation guardrail, exact live regression
        assert "m8" not in combined  # final completed descendant
        assert "m9" not in combined  # quoted classifier vocabulary below lead paragraph
    finally:
        dao.close()


@requires_frozen_fixtures
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


@requires_frozen_fixtures
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


@requires_frozen_fixtures
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


@requires_frozen_fixtures
def test_time_to_find_data_precondition():
    """Structural time-to-find precondition (spec T1/T3/T6): the target set is
    exactly the first screen (zero scroll) and the read is sub-100ms."""
    import time
    from debate_read_dao import DebateReadDAO

    dao = DebateReadDAO(FX_B, clock=asof_clock, forbid_path=PROD)
    try:
        for label, fn, expected in (
            ("recent", lambda: dao.recent(1.0, "CODEX_FIXTURE", ["DECISION", "STATE", "STATUS"])["items"], 10),
            ("waiting", lambda: dao.waiting_section_a(live_await=False)[0], 10),
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


def test_S2e_no_cas_or_dml_or_mutation_in_new_code():
    """Static S3-fence proof: the new debate code has no close/write/CAS
    capability — no apply_task_mutation, no CAS tokens, no raw DML on a
    persistent DB, no mutating handler (grep + AST)."""
    import re

    debate_mods = [os.path.join(REPO, m) for m in
                   ("debate_read_dao.py", "debate_list_widget.py")]
    # 1) AST: no banned calls/imports anywhere in the debate modules
    banned_calls = {"apply_task_mutation", "apply_task_mutation_cas",
                    "update_task", "mark_done", "delete_task"}
    banned_imports = {"db_utils", "subprocess", "close_task"}
    for m in debate_mods:
        imports, calls = (), set()
        import ast
        tree = ast.parse(open(m).read())
        imports = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imports.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                if n.module:
                    imports.add(n.module.split(".")[0])
            elif isinstance(n, ast.Call):
                f = n.func
                nm = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                calls.add(nm)
        assert not (imports & banned_imports), f"{m}: banned import {imports & banned_imports}"
        assert not (calls & banned_calls), f"{m}: banned call {calls & banned_calls}"

    # 2) CAS tokens must be absent from the new debate surface
    for m in debate_mods:
        src = open(m).read()
        for tok in ("expected_status", "expected_version", "expected_order",
                    "expected_event_id", "BEGIN IMMEDIATE", "ConflictError"):
            assert tok not in src, f"{m} must not reference CAS token {tok!r}"

    # 3) raw DML against the read-only fixture/prod connection is absent
    #    (only the ephemeral :memory: mirror is written, via a separate conn)
    dao_src = open(os.path.join(REPO, "debate_read_dao.py")).read()
    assert not re.search(r"self\._conn\.execute\(\s*[\"']\s*(INSERT|UPDATE|DELETE|CREATE|REPLACE)",
                         dao_src, re.I), "no DML on the read-only connection"

    # 4) the read-only tray helpers before the explicitly separated completion
    # adapter carry no mutation / subprocess. The adapter below this boundary
    # is allowed to carry the exact rendered CAS token for task/note rows only.
    tt = open(os.path.join(REPO, "task_tray.py")).read()
    block = tt[
        tt.index("def _debate_recent_params"):
        tt.index("def _task_completion_payload")
    ]
    for bad in ("apply_task_mutation", "update_task", "mark_done", "delete_task",
                "subprocess", "expected_status"):
        assert bad not in block, f"tray debate helpers must not reference {bad!r}"


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
    assert labels == [
        "Copy ID",
        "Copy full record",
        "Copy selected",
        "Copy all visible",
        "Open thread",
    ], (
        f"context menu must be read-only, got {labels}"
    )
    assert "Delete" not in labels and "Convert to Note" not in labels


def test_browser_equivalent_controls_filter_text_and_sort():
    from debate_list_widget import (
        apply_debate_controls,
        default_debate_control_params,
    )

    rows = [
        {"msg_id": "old-h", "ts": "2026-07-18T08:00:00Z", "priority": "H",
         "role": "CONDUCTOR", "kind": "Q", "line": "операторско решение",
         "body": "нужно е сега", "fwd": "чака оператор"},
        {"msg_id": "new-m", "ts": "2026-07-19T08:00:00Z", "priority": "M",
         "role": "ADVOCATE", "kind": "DECISION", "line": "UX verdict",
         "body": "точният текст", "fwd": "чака оператор"},
        {"msg_id": "new-h", "ts": "2026-07-19T09:00:00Z", "priority": "H",
         "role": "ADVOCATE", "kind": "Q", "line": "друг въпрос",
         "body": "без съвпадение", "fwd": "чака оператор"},
    ]
    params = default_debate_control_params("waiting")
    assert [r["msg_id"] for r in apply_debate_controls("waiting", rows, params)] == [
        "new-h", "new-m", "old-h"
    ]  # default web sort: ts DESC

    params["control_filters"] = {
        "priority": ["M"], "role": ["ADVOCATE"], "kind": ["DECISION"]
    }
    params["control_text"] = "ТОЧНИЯТ"
    assert [r["msg_id"] for r in apply_debate_controls("waiting", rows, params)] == [
        "new-m"
    ]

    params = default_debate_control_params("waiting")
    params["control_sort"] = "priority"
    params["control_dir"] = 1
    assert [r["priority"] for r in apply_debate_controls("waiting", rows, params)] == [
        "H", "H", "M"
    ]
    params["control_dir"] = -1
    assert [r["priority"] for r in apply_debate_controls("waiting", rows, params)] == [
        "M", "H", "H"
    ]


def test_recent_control_defaults_and_state_normalization():
    from debate_list_widget import normalize_debate_control_params

    state = normalize_debate_control_params("recent", {})
    assert state["hours"] == 24
    assert state["kinds"] == ["DECISION", "STATE", "STATUS"]
    assert state["control_sort"] == "ts" and state["control_dir"] == -1

    restored = normalize_debate_control_params("recent", {
        "hours": 72,
        "kinds": ["DECISION"],
        "control_sort": "role",
        "control_dir": 1,
        "control_text": "UX",
        "control_filters": {"priority": ["H"], "role": ["EXECUTOR3"]},
    })
    assert restored["hours"] == 72
    assert restored["kinds"] == ["DECISION"]
    assert restored["control_text"] == "UX"
    assert restored["control_filters"]["role"] == ["EXECUTOR3"]


def test_waiting_section_b_controls_match_browser_due_and_project_logic():
    from datetime import date, timedelta
    from debate_list_widget import (
        apply_debate_controls,
        default_debate_control_params,
    )

    today = date.today()
    rows = [
        {"id": "overdue", "title": "старо", "section": "today",
         "priority": "high", "project": "alpha",
         "due_date": (today - timedelta(days=1)).isoformat(), "updated_at": "1"},
        {"id": "week", "title": "тази седмица", "section": "next",
         "priority": "medium", "project": "alpha",
         "due_date": (today + timedelta(days=5)).isoformat(), "updated_at": "2"},
        {"id": "later", "title": "по-късно", "section": "next",
         "priority": "critical", "project": "beta",
         "due_date": (today + timedelta(days=15)).isoformat(), "updated_at": "3"},
        {"id": "none", "title": "без срок", "section": "today",
         "priority": "low", "project": "alpha", "due_date": "", "updated_at": "4"},
    ]
    params = default_debate_control_params("waiting_tasks")
    params["control_filters"] = {
        "priority": [], "project": ["alpha"], "section": [], "due": ["le7"]
    }
    assert [r["id"] for r in apply_debate_controls("waiting_tasks", rows, params)] == [
        "week"
    ]
    params["control_filters"]["due"] = ["none", "overdue"]
    assert [r["id"] for r in apply_debate_controls("waiting_tasks", rows, params)] == [
        "overdue", "none"
    ]


def test_reader_supports_partial_and_whole_copy(qapp):
    from debate_list_widget import DebateReaderDialog

    payload = {
        "title": "[Q] CONDUCTOR · abc",
        "body": "първа част\nвтора част",
        "record": "msg_id: abc\nrole: CONDUCTOR\n\nпърва част\nвтора част",
    }
    dlg = DebateReaderDialog(payload)
    try:
        # QPlainTextEdit is a native selectable reader: selecting a fragment and
        # invoking copy must copy only that fragment.
        cursor = dlg.body_view.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(5, cursor.MoveMode.KeepAnchor)
        dlg.body_view.setTextCursor(cursor)
        dlg.body_view.copy()
        assert qapp.clipboard().text() == "първа"

        dlg.copy_full_text()
        assert qapp.clipboard().text() == payload["body"]
        dlg.copy_full_record()
        assert qapp.clipboard().text() == payload["record"]
    finally:
        dlg.close()


def test_debate_list_ctrl_c_selected_or_all(qapp):
    from debate_list_widget import DebateListWidget

    w = DebateListWidget()
    first = w.add_debate_row("m1", "one", copy_payload="record one")
    second = w.add_debate_row("m2", "two", copy_payload="record two")
    first.setSelected(True)
    w.copy_selected()
    assert qapp.clipboard().text() == "record one"
    first.setSelected(False)
    second.setSelected(False)
    w.copy_selected()
    assert qapp.clipboard().text() == "record one\n\nrecord two"


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


@requires_frozen_fixtures
def test_BLOCKER1_fullwindow_debate_tabs_visible_and_load(qapp, tmp_path, monkeypatch):
    """Integration regression (audit 370313098246 BLOCKER 1): the three debate
    tabs must stay VISIBLE through FullWindow.__init__ → refresh() and actually
    load, not be hidden by the empty-`raw`-bucket visibility check."""
    import shutil
    import task_tray
    from PyQt6.QtCore import QSettings
    from debate_list_widget import DebateListWidget

    # Isolate QSettings to a temp ini (never touch the operator's real config).
    ini = str(tmp_path / "tray.ini")
    monkeypatch.setattr(
        task_tray, "QSettings",
        lambda *a, **k: QSettings(ini, QSettings.Format.IniFormat),
    )
    # No bridge/network side effects during construction.
    monkeypatch.setattr(task_tray.FullWindow, "_restore_profile_from_bridge",
                        lambda self: None, raising=False)

    # Throwaway READ-WRITE copy of FX-B — the frozen fixture is never opened rw.
    before = _sha256(FX_B)
    dbcopy = str(tmp_path / "fxb_copy.db")
    shutil.copyfile(FX_B, dbcopy)
    db = task_tray.TaskDB(dbcopy)
    fw = task_tray.FullWindow(db, sync_host=None)
    try:
        # __init__ already ran refresh(); all three debate tabs must be visible.
        expected_labels = {
            "recent": "Recent Decisions",
            "waiting": "Waiting on Me",
            "topics": "Debate by Topic",
        }
        for key in ("recent", "waiting", "topics"):
            idx = fw._tab_keys.index(key)
            assert fw.tabs.isTabVisible(idx), f"{key} tab hidden after refresh (BLOCKER 1)"
            assert fw.tabs.tabText(idx) == expected_labels[key]
        # Simulate a periodic refresh — they must STILL be visible.
        fw.refresh()
        for key in ("recent", "waiting", "topics"):
            idx = fw._tab_keys.index(key)
            assert fw.tabs.isTabVisible(idx), f"{key} tab hidden after periodic refresh"
        # Real load through the integration layer (DebateListWidget + DAO).
        for key in ("recent", "waiting", "topics"):
            fw._load_tab(key)
            assert isinstance(fw.tab_lists[key], DebateListWidget)
        # Seeded content reaches the widget (waiting Q's ≤21d + 10 topics fire
        # even under the live clock; recent 1h may be empty but is still shown).
        assert fw.tab_lists["waiting"].count() > 0
        assert fw.tab_lists["topics"].count() > 0
        # Exact browser waiting surface: independent controls and a second
        # read-only list for relevant today/next tasks.
        assert fw._waiting_task_controls is not None
        assert fw._waiting_task_list is not None
        assert fw._waiting_task_list.count() > 0

        # New native surfaces use the same user-owned appearance pipeline as
        # the established task tabs from the very first frame.
        import tray_dialogs as td
        expected_list_style = td._build_list_style()
        expected_surface_style = td._build_debate_surface_style()
        for key in ("recent", "waiting", "topics"):
            assert fw.tab_lists[key].styleSheet() == expected_list_style
            assert fw._debate_pages[key].styleSheet() == expected_surface_style
        assert fw._waiting_task_list.styleSheet() == expected_list_style

        # A later user appearance change propagates to all new tabs and their
        # secondary list; it is not a hard-coded one-off dark theme.
        old_appearance = (td._theme_name, td._font_size, td._bold)
        try:
            td._theme_name, td._font_size, td._bold = "light", 17, True
            fw._apply_debate_appearance()
            light_surface = td._build_debate_surface_style()
            light_list = td._build_list_style()
            assert "#f7fafc" in light_surface
            assert "font-size: 17px" in light_surface
            assert "font-weight: bold" in light_surface
            for key in ("recent", "waiting", "topics"):
                assert fw._debate_pages[key].styleSheet() == light_surface
                assert fw.tab_lists[key].styleSheet() == light_list
            assert fw._waiting_task_list.styleSheet() == light_list
        finally:
            td._theme_name, td._font_size, td._bold = old_appearance
            fw._apply_debate_appearance()

        # Per-tab control state is serialized with the existing QSettings view
        # profile, not lost when the user changes tabs/restarts the tray.
        state = fw._debate_controls["waiting"].state()
        state["control_text"] = "fixture waiting ask 1"
        state["control_sort"] = "role"
        state["control_dir"] = 1
        fw._on_debate_controls_changed("waiting", state)
        import json
        persisted = json.loads(fw._settings.value("tab_views"))
        assert persisted["waiting"]["params"]["control_text"] == "fixture waiting ask 1"
        assert persisted["waiting"]["params"]["control_sort"] == "role"
    finally:
        if getattr(fw, "_debate_dao", None) is not None:
            fw._debate_dao.close()
        fw.close()
        db._conn.close()
    assert _sha256(FX_B) == before, "FX-B frozen fixture must be untouched"


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
