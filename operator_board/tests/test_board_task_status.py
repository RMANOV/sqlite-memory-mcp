"""Gate A/B/C/D + prod-fence tests for the board "Завърши" (status->done) feature.

Task 5d4cc16c54ee (DAILY_20260704). Every test operates on a THROWAWAY DB under
pytest tmp_path; the real production DB is never opened for writing. The
prod-fence test additionally asserts the adapter refuses the canonical DB_PATH
and that a write-disabled board fail-closes.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import sqlite3
import sys
import threading

import pytest
from http.server import ThreadingHTTPServer

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BOARD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, _BOARD_DIR)

import board as B  # noqa: E402
from db_utils import DB_PATH, apply_task_mutation, create_task_with_ledger, get_conn_immediate  # noqa: E402
from schema import init_db  # noqa: E402
from task_status_cas import StatusToken, transition_status  # noqa: E402


# --------------------------------------------------------------------------- #
#  fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "tasks.db")
    init_db(path)
    return path


def _seed(db_path: str, tid: str, status: str = "not_started", *, type: str = "task",
          section: str = "today", title: str | None = None) -> None:
    with get_conn_immediate(db_path) as conn:
        create_task_with_ledger(
            conn, tid, title or f"Task {tid}", "2026-07-19T10:00:00+00:00",
            status=status, type=type, section=section, actor_id="test",
        )


def _row(db_path: str, tid: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id,type,status FROM tasks WHERE id=?", (tid,)
        ).fetchone()
    finally:
        conn.close()


def _version(db_path: str, tid: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(
            "SELECT updated_order, source_event_id FROM task_field_versions "
            "WHERE task_id=? AND field_name='status'", (tid,)
        ).fetchone()
        return (int(r["updated_order"]), r["source_event_id"]) if r else (0, None)
    finally:
        conn.close()


def _board(db_path: str, *, writes: bool = True) -> "B.Board":
    return B.Board(B.DB(db_path), enable_writes=writes)


def _done_payload(db_path: str, tid: str) -> dict:
    order, event = _version(db_path, tid)
    status = _row(db_path, tid)["status"]
    return {"id": tid, "action": "done", "expected_status": status,
            "expected_order": order, "expected_event_id": event}


# --------------------------------------------------------------------------- #
#  GATE A — functional CAS semantics
# --------------------------------------------------------------------------- #
def test_A_task_closes_exactly_once_and_unrelated_untouched(db_path):
    _seed(db_path, "t1", "in_progress")
    _seed(db_path, "t2", "not_started")
    bd = _board(db_path)
    res = bd.task_status(_done_payload(db_path, "t1"))
    assert res["ok"] and res["outcome"] == "applied"
    assert _row(db_path, "t1")["status"] == "done"
    assert _row(db_path, "t2")["status"] == "not_started"  # unrelated unchanged
    assert "undo_token" in res and res["undo_token"]["previous_status"] == "in_progress"


def test_A_note_currently_blocked_by_audited_adapter(db_path):
    # DISCOVERED CONSTRAINT (reported to gate): the merged CAS path
    # apply_task_mutation hard-restricts status transitions to type='task'
    # (db_utils.py:3740 not_task guard + :3758 "WHERE ... type='task'").
    # transition_status is therefore NOT type-agnostic for notes; closing a
    # note needs a blessed db_utils frozen-delta (type IN ('task','note')).
    # The board handler already type-validates {task,note}; it fails closed
    # here on the adapter's own guard rather than corrupting anything.
    _seed(db_path, "n1", "not_started", type="note")
    bd = _board(db_path)
    res = bd.task_status(_done_payload(db_path, "n1"))
    assert not res["ok"] and res["outcome"] == "conflict"
    assert res["reason"] == "not_task"
    assert _row(db_path, "n1")["status"] == "not_started"  # untouched, fail-closed


def test_A_undo_restores_exact_prior_status(db_path):
    _seed(db_path, "t1", "in_progress")
    bd = _board(db_path)
    done = bd.task_status(_done_payload(db_path, "t1"))
    ut = done["undo_token"]
    undo = bd.task_status({"id": "t1", "action": "undo",
                           "previous_status": ut["previous_status"],
                           "expected_status": ut["expected_status"],
                           "expected_order": ut["expected_order"],
                           "expected_event_id": ut["expected_event_id"]})
    assert undo["ok"] and undo["outcome"] == "applied"
    assert _row(db_path, "t1")["status"] == "in_progress"  # EXACT prior status


def test_A_wrong_type_rejected(db_path):
    _seed(db_path, "e1", "not_started", type="reference")
    bd = _board(db_path)
    res = bd.task_status(_done_payload(db_path, "e1"))
    assert not res["ok"] and res["reason"] == "wrong_type"
    assert _row(db_path, "e1")["status"] == "not_started"


def test_A_unknown_id_rejected(db_path):
    bd = _board(db_path)
    res = bd.task_status({"id": "nope", "action": "done", "expected_status": "not_started",
                          "expected_order": 1, "expected_event_id": "x"})
    assert not res["ok"] and res["reason"] == "not_found"


def test_A_stale_cas_rejected(db_path):
    _seed(db_path, "t1", "not_started")
    stale = _done_payload(db_path, "t1")
    # foreign advance after capturing the token
    with get_conn_immediate(db_path) as conn:
        apply_task_mutation(conn, "t1", {"status": "in_progress"},
                            actor_id="foreign", tool_name="test.foreign")
    bd = _board(db_path)
    res = bd.task_status(stale)
    assert not res["ok"] and res["outcome"] == "conflict"
    assert _row(db_path, "t1")["status"] == "in_progress"  # foreign change preserved


def test_A_double_click_idempotent(db_path):
    _seed(db_path, "t1", "not_started")
    payload = _done_payload(db_path, "t1")
    bd = _board(db_path)
    first = bd.task_status(dict(payload))
    assert first["ok"] and first["outcome"] == "applied"
    second = bd.task_status(dict(payload))  # same stale token, row already done
    assert second["ok"] and second["outcome"] == "noop" and second["reason"] == "already_done"


def test_A_undo_refuses_foreign_intermediate_change(db_path):
    _seed(db_path, "t1", "in_progress")
    bd = _board(db_path)
    ut = bd.task_status(_done_payload(db_path, "t1"))["undo_token"]
    # foreign change after done, before undo
    with get_conn_immediate(db_path) as conn:
        apply_task_mutation(conn, "t1", {"status": "archived"},
                            actor_id="foreign", tool_name="test.foreign")
    undo = bd.task_status({"id": "t1", "action": "undo",
                           "previous_status": ut["previous_status"],
                           "expected_status": ut["expected_status"],
                           "expected_order": ut["expected_order"],
                           "expected_event_id": ut["expected_event_id"]})
    assert not undo["ok"] and undo["outcome"] == "conflict"
    assert _row(db_path, "t1")["status"] == "archived"  # foreign change preserved


def test_A_invalid_version_rejected(db_path):
    _seed(db_path, "t1", "not_started")
    bd = _board(db_path)
    res = bd.task_status({"id": "t1", "action": "done", "expected_status": "not_started",
                          "expected_order": "notint", "expected_event_id": "x"})
    assert not res["ok"] and res["reason"] == "invalid_status_version"


# --------------------------------------------------------------------------- #
#  GATE B — HTTP boundary security (live loopback server)
# --------------------------------------------------------------------------- #
class _Srv:
    def __init__(self, db_path, *, writes=True, csrf="secret-nonce"):
        B.Handler.board = B.Board(B.DB(db_path), enable_writes=writes)
        B.Handler.csrf = csrf
        self.csrf = csrf
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), B.Handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def post(self, path, body=b"", ctype="application/json", csrf=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {}
        if ctype is not None:
            hdrs["Content-Type"] = ctype
        use = self.csrf if csrf is None else csrf
        if use is not False:
            hdrs["X-Board-CSRF"] = use
        payload = body if raw else json.dumps(body).encode()
        c.request("POST", path, body=payload, headers=hdrs)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            j = json.loads(data)
        except Exception:
            j = {"_raw": data.decode(errors="replace")}
        return r.status, j

    def stop(self):
        self.httpd.shutdown(); self.httpd.server_close()


@pytest.fixture
def srv(db_path):
    _seed(db_path, "t1", "not_started")
    s = _Srv(db_path)
    yield s, db_path
    s.stop()


def test_B_csrf_missing_rejected(srv):
    s, db_path = srv
    order, event = _version(db_path, "t1")
    code, j = s.post("/api/task-status",
                     {"id": "t1", "action": "done", "expected_status": "not_started",
                      "expected_order": order, "expected_event_id": event}, csrf=False)
    assert code == 403 and j["reason"] == "csrf"
    assert _row(db_path, "t1")["status"] == "not_started"


def test_B_csrf_wrong_rejected(srv):
    s, db_path = srv
    code, j = s.post("/api/task-status", {"id": "t1"}, csrf="WRONG")
    assert code == 403 and j["reason"] == "csrf"


def test_B_content_type_enforced(srv):
    s, _ = srv
    code, j = s.post("/api/task-status", {"id": "t1"}, ctype="text/plain")
    assert code == 415 and j["reason"] == "content_type"


def test_B_oversized_body_rejected(srv):
    s, _ = srv
    big = b'{"id":"' + b"x" * 5000 + b'"}'
    code, j = s.post("/api/task-status", big, raw=True)
    assert code == 413 and j["reason"] == "body_size"


def test_B_malformed_json_rejected(srv):
    s, _ = srv
    code, j = s.post("/api/task-status", b"{not json", raw=True)
    assert code == 400 and j["reason"] == "malformed_json"


def test_B_api_close_still_405_sentinel(srv):
    s, db_path = srv
    code, j = s.post("/api/close", {"id": "t1"})
    assert code == 405 and j.get("note") == "close_disabled_containment"
    assert _row(db_path, "t1")["status"] == "not_started"


def test_B_happy_path_over_loopback(srv):
    s, db_path = srv
    order, event = _version(db_path, "t1")
    code, j = s.post("/api/task-status",
                     {"id": "t1", "action": "done", "expected_status": "not_started",
                      "expected_order": order, "expected_event_id": event})
    assert code == 200 and j["ok"] and j["outcome"] == "applied"
    assert _row(db_path, "t1")["status"] == "done"


def test_B_loopback_predicate():
    h = B.Handler.__new__(B.Handler)
    for good in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        h.client_address = (good, 12345)
        assert h._client_is_loopback()
    for bad in ("10.0.0.5", "192.168.1.9", "8.8.8.8", ""):
        h.client_address = (bad, 12345)
        assert not h._client_is_loopback()


def test_B_static_no_subprocess_no_debate_dml():
    src = open(os.path.join(_BOARD_DIR, "board.py"), encoding="utf-8").read()
    assert "import subprocess" not in src
    assert "subprocess.run" not in src
    low = src.lower()
    for bad in ("update debate_messages", "delete from debate_messages",
                "insert into debate_messages"):
        assert bad not in low


# --------------------------------------------------------------------------- #
#  GATE C — UX regression (static structural proofs on the served PAGE)
# --------------------------------------------------------------------------- #
def test_C_page_preserves_ux_affordances():
    page = B.PAGE
    # copy (partial + whole), open reader, filters, sort, theme markers intact
    for marker in ("copybtn", "copyEl", "cardClick", "sortbtn", "txtfilter",
                   "--panel", "data-copy", "controlled("):
        assert marker in page, marker
    # done control is keyboard accessible + confirm defaults to cancel
    assert "donebtn" in page and 'role="button"' in page and "onkeydown" in page
    assert 'class="no"' in page and "no.focus()" in page  # default = Отказ focused
    # client no longer calls the contained /api/close
    assert "fetch('/api/close'" not in page
    # CSRF nonce placeholder present for injection
    assert "__BOARD_CSRF__" in page


def test_C_csrf_injected_on_get(db_path):
    _seed(db_path, "t1", "not_started")
    s = _Srv(db_path, csrf="inject-me-123")
    try:
        c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
        c.request("GET", "/")
        r = c.getresponse(); html_out = r.read().decode(); c.close()
        assert "inject-me-123" in html_out and "__BOARD_CSRF__" not in html_out
    finally:
        s.stop()


# --------------------------------------------------------------------------- #
#  GATE D — prod fence
# --------------------------------------------------------------------------- #
def _sha(path: str):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def test_D_writes_disabled_fail_closed(db_path):
    _seed(db_path, "t1", "not_started")
    bd = _board(db_path, writes=False)
    res = bd.task_status(_done_payload(db_path, "t1"))
    assert not res["ok"] and res["outcome"] == "writes_disabled"
    assert _row(db_path, "t1")["status"] == "not_started"


def test_D_adapter_refuses_canonical_db_path():
    # The audited adapter refuses the real DB_PATH when it is fenced.
    tok = StatusToken("whatever", "not_started", 1, "evt")
    with pytest.raises(PermissionError):
        transition_status(DB_PATH, tok, "done", forbid_path=DB_PATH)


def test_D_prod_db_untouched_by_feature(db_path):
    # Structural + best-effort hash proof: exercising the feature on a throwaway
    # DB does not change the production DB / JSONL files.
    prod_files = [DB_PATH,
                  os.path.expanduser("~/.claude/memory/memory.db-wal"),
                  os.path.join(_BOARD_DIR, "closed_tasks.jsonl")]
    before = {p: _sha(p) for p in prod_files}
    _seed(db_path, "t1", "in_progress")
    bd = _board(db_path)  # throwaway, writes enabled
    done = bd.task_status(_done_payload(db_path, "t1"))
    assert done["ok"]
    ut = done["undo_token"]
    bd.task_status({"id": "t1", "action": "undo", "previous_status": ut["previous_status"],
                    "expected_status": ut["expected_status"], "expected_order": ut["expected_order"],
                    "expected_event_id": ut["expected_event_id"]})
    after = {p: _sha(p) for p in prod_files}
    # The main prod DB and the JSONL never move because of this feature run.
    assert before[DB_PATH] == after[DB_PATH], "prod memory.db hash changed"
    jsonl = os.path.join(_BOARD_DIR, "closed_tasks.jsonl")
    assert before[jsonl] == after[jsonl], "closed_tasks.jsonl changed"
