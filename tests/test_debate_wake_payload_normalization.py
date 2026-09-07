"""PostToolUse payload normalization for ``hooks/debate_wake.py``.

Incident (routing repair, lane R): the wake hook logged
``missing_tool_response`` for posts made through a different MCP client even
though the hook payload carried a ``tool_response`` key. The hook then exited
0 without resolving any wake target, so nothing was dispatched.

The debate post tool returns one JSON object. Depending on the MCP client and
Claude hook version that object reaches the hook in one of several wrappers:

* the parsed object itself (``{"msg_id": ...}``);
* ``{"result": "<json string>"}`` (observed in interactive transcripts);
* an MCP content list ``[{"type": "text", "text": "<json string>"}]``;
* an MCP result envelope ``{"content": [...], "structuredContent": {...}}``;
* a plain JSON string;
* alternate hook keys ``toolResponse`` / ``tool_result`` / ``toolResult``.

Contract pinned here: the normalizer returns the parsed post result (with
``msg_id`` and ``topic_id``) for every realistic wrapper and ``None`` only
when no post result is present at all. Every test loads the hook module from
source and never opens the live database.
"""

from __future__ import annotations

import io
import json
import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MSG_ID = "a1b2c3d4e5f6"
TOPIC_ID = "WAKE_PAYLOAD_T1"
POST_RESULT = {
    "msg_id": MSG_ID,
    "ts": "2026-08-26T04:12:02.030451Z",
    "recipient_count": 2,
    "diagnostic_recipient_count": 0,
    "topic_state": "ACTIVE",
    "vehicle": "implementation",
    "topic_id": TOPIC_ID,
    "schema_version": "debate_post_with_recipients.v1",
}
POST_JSON = json.dumps(POST_RESULT)
TOOL_NAME = "mcp__sqlite_intel__debate_post_with_recipients"


def _load_wake_module(tmp_path: Path, name: str):
    # LOG_PATH is resolved from the environment at import time; point it at
    # the temp dir before the module body runs so no test touches ~/.claude.
    os.environ["DEBATE_WAKE_HOOK_LOG"] = str(tmp_path / "wake_hook.jsonl")
    os.environ["DEBATE_WAKE_AGENT_LOG_DIR"] = str(tmp_path / "agents")
    spec = spec_from_file_location(name, ROOT / "hooks" / "debate_wake.py")
    module = module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wake(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBATE_WAKE_HOOK_LOG", str(tmp_path / "wake_hook.jsonl"))
    monkeypatch.setenv("DEBATE_WAKE_AGENT_LOG_DIR", str(tmp_path / "agents"))
    return _load_wake_module(tmp_path, "debate_wake_payload_normalization_test")


def _events(module) -> list[dict]:
    path = Path(module.LOG_PATH)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── shapes that must all normalize to the parsed post result ────────────────

ACCEPTED_SHAPES = {
    # Already accepted before this change (no behaviour change allowed).
    "parsed_dict": dict(POST_RESULT),
    "result_json_string": {"result": POST_JSON},
    "plain_json_string": POST_JSON,
    "result_json_string_as_string": json.dumps({"result": POST_JSON}),
    # MCP content-block list (Claude hook ``tool_response`` for MCP tools).
    "content_block_list": [{"type": "text", "text": POST_JSON}],
    # MCP result envelope with ``content`` only.
    "content_envelope": {"content": [{"type": "text", "text": POST_JSON}]},
    # MCP result envelope with ``content`` + ``structuredContent`` + isError.
    "content_and_structured": {
        "content": [{"type": "text", "text": POST_JSON}],
        "structuredContent": dict(POST_RESULT),
        "isError": False,
    },
    # FastMCP wraps non-object returns as {"result": ...} in structuredContent.
    "structured_content_result_wrapper": {
        "structuredContent": {"result": POST_JSON},
    },
    "structured_content_direct": {"structuredContent": dict(POST_RESULT)},
    "structured_content_snake": {"structured_content": dict(POST_RESULT)},
    # Nested wrapper: a content block whose text is itself {"result": "<json>"}.
    "content_block_double_wrapped": [
        {"type": "text", "text": json.dumps({"result": POST_JSON})}
    ],
    # A leading human-readable block must not hide the JSON block after it.
    "content_block_after_prose": [
        {"type": "text", "text": "Posted."},
        {"type": "text", "text": POST_JSON},
    ],
    # Nested result dict (not a string).
    "result_dict": {"result": dict(POST_RESULT)},
}


@pytest.mark.parametrize("shape_name", sorted(ACCEPTED_SHAPES))
def test_tool_response_shapes_normalize_to_post_result(wake, shape_name):
    payload = {"tool_name": TOOL_NAME, "tool_response": ACCEPTED_SHAPES[shape_name]}
    out = wake._extract_tool_response(payload)
    assert isinstance(out, dict), shape_name
    assert out["msg_id"] == MSG_ID
    assert out["topic_id"] == TOPIC_ID
    assert out["schema_version"] == "debate_post_with_recipients.v1"


@pytest.mark.parametrize(
    "key", ["tool_response", "toolResponse", "tool_result", "toolResult", "tool_output"]
)
def test_hook_payload_key_variants_are_accepted(wake, key):
    payload = {"tool_name": TOOL_NAME, key: [{"type": "text", "text": POST_JSON}]}
    out = wake._extract_tool_response(payload)
    assert isinstance(out, dict)
    assert out["msg_id"] == MSG_ID


def test_top_level_post_result_is_still_accepted(wake):
    # Scan/replay paths feed the post result directly (no hook envelope).
    out = wake._extract_tool_response(dict(POST_RESULT))
    assert out is not None and out["msg_id"] == MSG_ID


# ── genuinely absent responses must stay None ───────────────────────────────

ABSENT_SHAPES = {
    "no_response_key": {"tool_name": TOOL_NAME, "tool_input": {"topic_id": TOPIC_ID}},
    "empty_dict": {"tool_name": TOOL_NAME, "tool_response": {}},
    "empty_list": {"tool_name": TOOL_NAME, "tool_response": []},
    "empty_string": {"tool_name": TOOL_NAME, "tool_response": ""},
    "prose_string": {"tool_name": TOOL_NAME, "tool_response": "posted ok"},
    "prose_block": {
        "tool_name": TOOL_NAME,
        "tool_response": [{"type": "text", "text": "posted ok"}],
    },
    "empty_content": {"tool_name": TOOL_NAME, "tool_response": {"content": []}},
    "none_value": {"tool_name": TOOL_NAME, "tool_response": None},
    # A typed tool error carries no post result: nothing to route.
    "error_result": {
        "tool_name": TOOL_NAME,
        "tool_response": {
            "result": json.dumps(
                {"error": "diagnostic_binding_required", "error_type": "x"}
            )
        },
    },
    "unrelated_dict": {"tool_name": TOOL_NAME, "tool_response": {"ok": True}},
}


@pytest.mark.parametrize("shape_name", sorted(ABSENT_SHAPES))
def test_absent_or_non_post_responses_return_none(wake, shape_name):
    assert wake._extract_tool_response(ABSENT_SHAPES[shape_name]) is None


def test_normalizer_is_bounded_on_pathological_nesting(wake):
    value: object = POST_JSON
    for _ in range(64):
        value = {"result": value}
    # Either it resolves or it fails closed; it must never raise/recurse away.
    out = wake._extract_tool_response({"tool_name": TOOL_NAME, "tool_response": value})
    assert out is None or out["msg_id"] == MSG_ID


# ── end-to-end through _run_hook ─────────────────────────────────────────────


def _run_hook_with(module, monkeypatch, payload: dict) -> list[dict]:
    seen: list[dict] = []
    monkeypatch.setattr(
        module,
        "_handle_tool_response",
        lambda tool_response: seen.append(dict(tool_response)) or {"targets": []},
    )
    monkeypatch.setattr(module, "_maybe_dispatch", lambda *_a, **_k: {"launches": []})
    monkeypatch.setattr(module, "_agent_resolution_disabled", lambda _tr: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert module._run_hook() == 0
    return seen


def test_run_hook_resolves_content_block_list_payload(wake, monkeypatch):
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "s1",
        "tool_name": TOOL_NAME,
        "tool_input": {"topic_id": TOPIC_ID},
        "tool_response": [{"type": "text", "text": POST_JSON}],
    }
    seen = _run_hook_with(wake, monkeypatch, payload)
    assert [s["msg_id"] for s in seen] == [MSG_ID]
    assert not [e for e in _events(wake) if e["event"] == "missing_tool_response"]


def test_run_hook_logs_missing_response_with_shape_for_absent_payload(
    wake, monkeypatch
):
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": TOOL_NAME,
        "tool_response": [{"type": "text", "text": "not json"}],
    }
    seen = _run_hook_with(wake, monkeypatch, payload)
    assert seen == []
    missing = [e for e in _events(wake) if e["event"] == "missing_tool_response"]
    assert len(missing) == 1
    # The shape hint makes the next incident diagnosable from the log alone.
    assert "response_shape" in missing[0]
