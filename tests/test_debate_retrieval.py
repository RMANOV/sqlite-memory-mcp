from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

import pytest

from debate_prompt_context import rank_pending_from_memory_db
from debate_retrieval import search_debate_context
from schema import init_db


GOLDEN_PATH = Path(__file__).parent / "fixtures" / "debate_retrieval_golden.json"


@pytest.fixture
def retrieval_db(tmp_path):
    spec = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    db_path = tmp_path / "memory.db"
    init_db(str(db_path))
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute(
        "INSERT INTO debates "
        "(topic_id,title,state,created_at,created_by_role,roles_json,metadata_json) "
        "VALUES ('RETRIEVAL1','golden retrieval','ACTIVE',"
        "'2026-07-19T08:00:00Z','CONDUCTOR',?,?)",
        (
            json.dumps(
                [
                    {"role": "CONDUCTOR", "session_id": "cc-conductor1"},
                    {"role": "EXECUTOR", "session_id": "codex-executor1"},
                    {"role": "ADVOCATE", "session_id": "cc-advocate1"},
                ]
            ),
            json.dumps({"priority_lane": "P1"}),
        ),
    )
    for index, message in enumerate(spec["messages"], start=1):
        ts = f"2026-07-19T08:{index:02d}:00Z"
        con.execute(
            "INSERT INTO debate_messages "
            "(msg_id,topic_id,role,ts,priority,kind,standing,vehicle,reply_to,body,created_at) "
            "VALUES (?,'RETRIEVAL1',?,?,?,?,NULL,'analysis',NULL,?,?)",
            (
                message["msg_id"],
                message["role"],
                ts,
                message["priority"],
                message["kind"],
                message["body"],
                ts,
            ),
        )
        con.execute(
            "INSERT INTO debate_message_recipients "
            "(msg_id,recipient,recipient_mode) VALUES (?,?,'normal')",
            (message["msg_id"], message["recipient"]),
        )
    con.commit()
    con.close()
    return db_path, spec


def _search(db_path: Path, **kwargs):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return search_debate_context(con, topic_ids=["RETRIEVAL1"], **kwargs)
    finally:
        con.close()


def test_golden_set_has_ten_preregistered_queries_and_expected_top_k(retrieval_db):
    db_path, spec = retrieval_db
    assert len(spec["queries"]) >= 10
    assert spec["contract"]["top_k"] == 1
    for case in spec["queries"]:
        out = _search(
            db_path,
            query=case["query"],
            limit=spec["contract"]["top_k"],
            snippet_bytes=spec["contract"]["snippet_cap_bytes"],
            max_query_ms=spec["contract"]["query_deadline_ms"],
        )
        assert [item["msg_id"] for item in out["results"]] == case["expected"]


def test_same_query_and_db_state_serializes_byte_identically_three_times(retrieval_db):
    db_path, _spec = retrieval_db
    receipts = [
        json.dumps(
            _search(db_path, query="RRF fusion", limit=10),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        for _ in range(3)
    ]
    assert receipts[0] == receipts[1] == receipts[2]


def test_both_paths_merge_with_weighted_rrf_and_recall_superset(retrieval_db):
    db_path, spec = retrieval_db
    out = _search(db_path, query="regression gate", limit=100, per_path_limit=100)
    assert out["merge"] == "weighted_rrf"
    assert out["paths"]["fts_bm25"] > 0
    assert out["paths"]["literal_metadata"] > 0
    fused_ids = {item["msg_id"] for item in out["results"]}
    literal_ids = {
        item["msg_id"]
        for item in out["results"]
        if "literal_metadata" in item["source_ranks"]
    }
    assert literal_ids <= fused_ids
    assert len(fused_ids) <= len(spec["messages"])


def test_candidate_scope_cannot_leak_unaddressed_messages(retrieval_db):
    db_path, spec = retrieval_db
    pending = [
        {
            "msg_id": spec["messages"][0]["msg_id"],
            "topic_id": "RETRIEVAL1",
            "role": "EXECUTOR",
            "ts": "2026-07-19T08:01:00Z",
            "priority": "H",
            "kind": "STATUS",
            "reply_to": None,
            "body": spec["messages"][0]["body"],
            "created_at": "2026-07-19T08:01:00Z",
        }
    ]
    ranked = rank_pending_from_memory_db(
        db_path=db_path,
        pending=pending,
        query="background filler alpha checksum",
        role="CONDUCTOR",
        session_id="cc-conductor1",
        limit=8,
    )
    assert [item["msg_id"] for item in ranked] == ["a00000000001"]


def test_snippets_are_capped_by_utf8_bytes(retrieval_db):
    db_path, _spec = retrieval_db
    con = sqlite3.connect(db_path)
    try:
        body = "начало " + ("многобайтов текст " * 100) + " край"
        con.execute(
            "INSERT INTO debate_messages VALUES "
            "('a00000000013','RETRIEVAL1','EXECUTOR','2026-07-19T08:13:00Z',"
            "'H','STATUS',NULL,'analysis',NULL,?,'2026-07-19T08:13:00Z')",
            (body,),
        )
        con.commit()
    finally:
        con.close()
    out = _search(db_path, query="многобайтов", limit=1, snippet_bytes=120)
    snippet = out["results"][0]["snippet"]
    assert len(snippet.encode("utf-8")) <= 120
    snippet.encode("utf-8").decode("utf-8")


def test_fts_triggers_track_insert_update_and_delete(retrieval_db):
    db_path, _spec = retrieval_db
    con = sqlite3.connect(db_path)
    try:
        assert con.execute(
            "SELECT count(*) FROM debate_messages_fts WHERE msg_id='a00000000001'"
        ).fetchone()[0] == 1
        con.execute(
            "UPDATE debate_messages SET body='changed exact token' "
            "WHERE msg_id='a00000000001'"
        )
        con.commit()
        assert con.execute(
            "SELECT count(*) FROM debate_messages_fts "
            "WHERE debate_messages_fts MATCH 'changed' AND msg_id='a00000000001'"
        ).fetchone()[0] == 1
        con.execute("DELETE FROM debate_messages WHERE msg_id='a00000000001'")
        con.commit()
        assert con.execute(
            "SELECT count(*) FROM debate_messages_fts WHERE msg_id='a00000000001'"
        ).fetchone()[0] == 0
    finally:
        con.close()


def test_search_runs_on_read_only_query_only_connection_with_bounded_runtime(retrieval_db):
    db_path, spec = retrieval_db
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    started = time.monotonic()
    try:
        out = search_debate_context(
            con,
            query="watchdog",
            topic_ids=["RETRIEVAL1"],
            limit=5,
            max_query_ms=spec["contract"]["query_deadline_ms"],
        )
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM debate_messages")
    finally:
        con.close()
    assert out["results"][0]["msg_id"] == "b00000000002"
    assert time.monotonic() - started < 2.0


def test_retrieval_module_has_no_llm_in_loop():
    source = (Path(__file__).parent.parent / "debate_retrieval.py").read_text(
        encoding="utf-8"
    ).casefold()
    forbidden = ("openai", "anthropic", "litellm", "chat.completions", "responses.create")
    assert not any(token in source for token in forbidden)
