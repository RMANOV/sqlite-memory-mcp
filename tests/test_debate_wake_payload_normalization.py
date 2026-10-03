"""Synthetic wrapper/privacy contract; no production database or launcher."""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POST = {"msg_id": "a1b2c3d4e5f6", "topic_id": "NORMALIZE_T1",
        "schema_version": "debate_post_with_recipients.v1"}
TOOL = "mcp__sqlite_intel__debate_post_with_recipients"


@pytest.fixture
def wake(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBATE_WAKE_HOOK_LOG", str(tmp_path / "wake.jsonl"))
    monkeypatch.setenv("DEBATE_WAKE_AGENT_LOG_DIR", str(tmp_path / "agents"))
    spec = spec_from_file_location("normalization_hook_test", ROOT / "hooks/debate_wake.py")
    mod = module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("wrapper", [
    lambda p: p, lambda p: json.dumps(p), lambda p: {"result": json.dumps(p)},
    lambda p: [{"type": "text", "text": json.dumps(p)}],
    lambda p: {"content": [{"type": "text", "text": json.dumps(p)}]},
    lambda p: {"structuredContent": {"result": json.dumps(p)}},
    lambda p: {"structured_content": p},
    lambda p: {"content": [{"text": "Posted."}, {"text": json.dumps(p)}]},
    lambda p: {"content": [{"text": json.dumps(p)}], "structuredContent": p, "isError": False},
])
def test_positive_wrappers(wake, wrapper):
    assert wake._extract_tool_response({"tool_response": wrapper(dict(POST))}) == POST


@pytest.mark.parametrize("key", ["toolResponse", "tool_result", "toolResult", "tool_output", "response", "result"])
def test_legacy_hook_alias(wake, key):
    assert wake._extract_tool_response({key: {"content": [{"text": json.dumps(POST)}]}}) == POST


def test_canonical_null_excludes_stale_alias(wake):
    assert wake._extract_tool_response({"tool_response": None, "result": POST}) is None


def test_error_envelope_rejects_valid_identity(wake):
    assert wake._extract_tool_response({"tool_response": {"isError": True, "result": POST}}) is None


@pytest.mark.parametrize("field,value", [("msg_id", "b1b2c3d4e5f6"), ("topic_id", "OTHER"), ("schema_version", "other.v1")])
def test_conflicting_candidates_rejected(wake, field, value):
    other = dict(POST, **{field: value})
    assert wake._extract_tool_response({"tool_response": {"result": POST, "structuredContent": other}}) is None


def test_most_complete_original_candidate_selected(wake):
    reduced = {"msg_id": POST["msg_id"]}
    full = dict(POST)
    result = wake._extract_tool_response({"tool_response": {"result": reduced, "structuredContent": full}})
    assert result is full


def test_reduced_legacy_identity_remains_normalizable(wake):
    reduced = {"msg_id": POST["msg_id"]}
    assert wake._extract_tool_response({"tool_response": reduced}) is reduced


@pytest.mark.parametrize("field", ["msg_id", "topic_id", "schema_version"])
@pytest.mark.parametrize("invalid", [None, "", [], {}, True])
def test_invalid_identity_rejects_envelope(wake, field, invalid):
    assert wake._extract_tool_response({"tool_response": dict(POST, **{field: invalid})}) is None


@pytest.mark.parametrize("kind", ["depth", "nodes", "list", "json_bytes"])
def test_bounds_reject_entire_envelope(wake, kind):
    if kind == "depth":
        excessive = POST
        for _ in range(14):
            excessive = {"result": excessive}
    elif kind == "nodes":
        excessive = {"result": [{"result": [None] * 64} for _ in range(5)]}
    elif kind == "list":
        excessive = [None] * 65
    else:
        excessive = json.dumps({"padding": "x" * 262144})
    # An early success cannot conceal a later branch over its bound.
    assert wake._extract_tool_response({"tool_response": {"result": POST, "content": excessive}}) is None


def test_shape_never_discloses_unknown_keys_or_values(wake):
    shape = wake._describe_shape({"PRIVATE_KEY_SENTINEL": "PRIVATE_VALUE_SENTINEL", "result": {"OTHER_PRIVATE_KEY": "PRIVATE_VALUE_SENTINEL"}})
    encoded = json.dumps(shape)
    assert "PRIVATE" not in encoded
    assert shape["unknown_key_count"] == 1
    assert "result" in encoded


def test_missing_payload_log_private_keys_and_tool_prefix(wake, monkeypatch, capsys):
    payload = {"PRIVATE_KEY_SENTINEL": "PRIVATE_VALUE_SENTINEL",
               "tool_name": "PRIVATE_TOOL_SENTINEL" + wake.TARGET_TOOL_SUFFIX,
               "tool_response": {"result": {"NESTED_PRIVATE_SENTINEL": "PRIVATE_VALUE_SENTINEL"}}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert wake._run_hook() == 0
    logged = wake.LOG_PATH.read_text()
    assert "PRIVATE" not in logged
    assert "missing_tool_response" in logged
    assert "response_shape" in logged
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err


@pytest.mark.parametrize("payload", [[], "text", None, 3])
def test_nonobject_hook_payload_fails_closed(wake, monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert wake._run_hook() == 0


def test_actual_dao_resolution_authoritative_row(wake, tmp_path, monkeypatch):
    # Uses the production resolver and DB seam, with an explicit synthetic DB.
    sys.path.insert(0, str(ROOT))
    from schema import init_db
    import db_utils
    from debate import init_debate, transition_state, bind_role_session, debate_post_with_recipients
    db = tmp_path / "normalization.db"
    monkeypatch.setenv("SQLITE_MEMORY_DB", str(db))
    monkeypatch.setattr(db_utils, "DB_PATH", str(db))
    monkeypatch.setenv("DEBATE_WAKE_ACTION", "dry_run")
    init_db(str(db))
    con = sqlite3.connect(db, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        init_debate(con, topic_id="NORMALIZE_T1", title="synthetic", roles=[{"role": "CONDUCTOR", "session_id": "s-cond"}, {"role": "EXECUTOR", "session_id": "s-exec"}], created_by_role="CONDUCTOR")
        transition_state(con, topic_id="NORMALIZE_T1", role="CONDUCTOR", new_state="ACTIVE")
        bind_role_session(con, topic_id="NORMALIZE_T1", role="EXECUTOR", session_id="cc-normalization123", runtime="cc", reason="synthetic fixture")
        posted = debate_post_with_recipients(con, topic_id="NORMALIZE_T1", role="CONDUCTOR", priority="H", kind="Q", body="synthetic analysis", addressed_to=["EXECUTOR"], vehicle="analysis")
        actual = wake._extract_tool_response({"tool_response": {"content": [{"text": json.dumps(posted)}]}})
        resolved = wake._handle_tool_response(actual)
        assert [t["target_session_id"] for t in resolved["targets"]] == ["cc-normalization123"]
        assert wake._maybe_dispatch(actual, resolved)["launches"] == []
        # Exercise actual stdin hook -> normalization -> resolver -> dry-run.
        monkeypatch.setenv("DEBATE_WAKE_ACTION_NAME", "normalization_stdin_integration")
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": TOOL, "tool_response": {"structuredContent": posted, "content": [{"text": json.dumps(posted)}]}})))
        assert wake._run_hook() == 0
        events = [json.loads(line) for line in wake.LOG_PATH.read_text().splitlines()]
        assert any(e["event"] == "wake_resolved" and e["msg_id"] == posted["msg_id"] for e in events)
        assert not any(e["event"] == "wake_resolution_failed" for e in events)
        assert con.execute("SELECT result FROM debate_wake_log WHERE trigger_msg_id=? AND action=?", (posted["msg_id"], "normalization_stdin_integration")).fetchone()[0] == "dry_run"
        mutated = dict(actual, msg_id="ffffffffffff")
        unknown = wake._handle_tool_response(mutated)
        assert unknown["targets"] == []
        assert wake._maybe_dispatch(mutated, unknown)["launches"] == []
    finally:
        con.close()
