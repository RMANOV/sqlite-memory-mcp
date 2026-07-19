"""Safe prompt adapter for ranked debate inbox context.

Addressing/unread status is established by the signal DAO first.  This module
then ranks only those proven candidate message ids through the two-path
``debate_retrieval`` engine and replaces full bodies with bounded snippets.
The production memory database is opened ``mode=ro`` with ``query_only=ON``.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any, Sequence

from debate_retrieval import search_debate_context


def rank_pending_from_memory_db(
    *,
    db_path: Path | str,
    pending: Sequence[dict[str, Any]],
    query: str,
    role: str,
    session_id: str,
    limit: int = 8,
    snippet_bytes: int = 480,
    max_query_ms: int = 750,
) -> list[dict[str, Any]]:
    """Rank an authoritative pending set without exposing full message bodies."""
    if not pending:
        return []
    by_id = {str(item["msg_id"]): dict(item) for item in pending}
    topic_ids = sorted(
        {str(item.get("topic_id") or "") for item in pending if item.get("topic_id")}
    )
    uri = f"file:{Path(db_path).expanduser().resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=1.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA busy_timeout=1000")
        ranked = search_debate_context(
            con,
            query=query,
            topic_ids=topic_ids,
            candidate_msg_ids=list(by_id),
            target_role=role,
            target_session_id=session_id,
            limit=limit,
            snippet_bytes=snippet_bytes,
            max_query_ms=max_query_ms,
        )
    finally:
        con.close()

    out: list[dict[str, Any]] = []
    for hit in ranked["results"]:
        item = by_id[str(hit["msg_id"])]
        item["body"] = hit["snippet"]
        item["retrieval"] = {
            "rank": hit["rank"],
            "score": hit["score"],
            "source_ranks": hit["source_ranks"],
            "body_bytes": hit["body_bytes"],
            "snippet_bytes": len(hit["snippet"].encode("utf-8")),
        }
        out.append(item)
    return out
