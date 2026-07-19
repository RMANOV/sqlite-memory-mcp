"""Bounded hybrid retrieval for the debate ledger in ``memory.db``.

The debate prompt path must not dump a FIFO slice of long message bodies.  It
needs two complementary native-SQLite retrieval paths:

* FTS5/BM25 for token relevance (including Bulgarian via ``unicode61``);
* literal + structural matching for ids, paths, quoted phrases, priorities,
  recipients, recency, and unresolved questions.

The paths are merged with weighted Reciprocal Rank Fusion (RRF), then receive
small, explicit debate-specific boosts.  Results expose bounded snippets, not
full bodies, so callers can safely use them in prompt and notification surfaces.
No optional vector dependency is required; both paths run inside the canonical
SQLite memory database.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence


RRF_K = 60
MAX_QUERY_TOKENS = 16
DEFAULT_SNIPPET_BYTES = 480
DEFAULT_MAX_QUERY_MS = 750
_TOKEN_RE = re.compile(r"[^\W_]", re.UNICODE)
_WORD_RE = re.compile(r"[\w-]{2,}", re.UNICODE)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def query_tokens(query: str) -> list[str]:
    """Return stable, case-folded query tokens suitable for FTS quoting."""
    return _dedupe(
        token.casefold()
        for token in _WORD_RE.findall(str(query or ""))
        if _TOKEN_RE.search(token)
    )[:MAX_QUERY_TOKENS]


def _fts_query(tokens: Sequence[str]) -> str:
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _where_in(column: str, values: Sequence[str]) -> tuple[str, list[str]]:
    clean = _dedupe(values)
    if not clean:
        return "", []
    return f" AND {column} IN ({','.join('?' for _ in clean)})", clean


def _row_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    recipients = str(item.pop("recipients", "") or "")
    item["recipients"] = sorted(
        value for value in recipients.split(",") if value
    )
    return item


def _fts_path(
    conn: sqlite3.Connection,
    *,
    tokens: Sequence[str],
    topic_ids: Sequence[str],
    candidate_msg_ids: Sequence[str],
    limit: int,
) -> list[dict[str, Any]]:
    if not tokens:
        return []
    topic_sql, topic_params = _where_in("topic_id", topic_ids)
    candidate_sql, candidate_params = _where_in("msg_id", candidate_msg_ids)
    try:
        rows = conn.execute(
            "WITH hits AS ("
            " SELECT msg_id, bm25(debate_messages_fts, 0.0, 0.0, 0.35, 0.2, 1.0) AS lexical_rank"
            " FROM debate_messages_fts"
            " WHERE debate_messages_fts MATCH ?"
            f" {topic_sql} {candidate_sql}"
            " ORDER BY lexical_rank ASC, msg_id ASC LIMIT ?"
            ") "
            "SELECT m.msg_id, m.topic_id, m.role, m.ts, m.priority, m.kind,"
            " m.reply_to, m.standing, COALESCE(m.vehicle,'analysis') AS vehicle,"
            " m.body, m.created_at, h.lexical_rank,"
            " GROUP_CONCAT(DISTINCT r.recipient) AS recipients"
            " FROM hits h JOIN debate_messages m ON m.msg_id = h.msg_id"
            " LEFT JOIN debate_message_recipients r ON r.msg_id = m.msg_id"
            " GROUP BY m.msg_id ORDER BY h.lexical_rank ASC, m.ts DESC, m.msg_id DESC",
            [_fts_query(tokens), *topic_params, *candidate_params, limit],
        ).fetchall()
    except sqlite3.Error:
        # Legacy/partial installs still have the literal path.  init_db repairs
        # the FTS index, but retrieval itself remains read-only and fail-soft.
        return []
    return [_row_payload(row) for row in rows]


def _literal_candidates(
    conn: sqlite3.Connection,
    *,
    topic_ids: Sequence[str],
    candidate_msg_ids: Sequence[str],
    scan_limit: int,
) -> list[dict[str, Any]]:
    topic_sql, topic_params = _where_in("m.topic_id", topic_ids)
    candidate_sql, candidate_params = _where_in("m.msg_id", candidate_msg_ids)
    rows = conn.execute(
        "SELECT m.msg_id, m.topic_id, m.role, m.ts, m.priority, m.kind,"
        " m.reply_to, m.standing, COALESCE(m.vehicle,'analysis') AS vehicle,"
        " m.body, m.created_at, GROUP_CONCAT(DISTINCT r.recipient) AS recipients"
        " FROM debate_messages m"
        " LEFT JOIN debate_message_recipients r ON r.msg_id = m.msg_id"
        f" WHERE 1=1 {topic_sql} {candidate_sql}"
        " GROUP BY m.msg_id ORDER BY m.ts DESC, m.msg_id DESC LIMIT ?",
        [*topic_params, *candidate_params, scan_limit],
    ).fetchall()
    return [_row_payload(row) for row in rows]


def _literal_score(
    item: dict[str, Any], query: str, tokens: Sequence[str]
) -> float:
    if not query and not tokens:
        return 1.0
    # This is deliberately independent from the FTS index.  Exact ids,
    # recipients, paths, quoted phrases, and structural fields remain
    # discoverable even when tokenisation is a poor fit for the query.
    searchable = "\n".join(
        [
            str(item.get("msg_id") or ""),
            str(item.get("topic_id") or ""),
            str(item.get("role") or ""),
            str(item.get("priority") or ""),
            str(item.get("kind") or ""),
            str(item.get("reply_to") or ""),
            str(item.get("vehicle") or ""),
            " ".join(item.get("recipients") or []),
            str(item.get("body") or ""),
        ]
    )
    folded = searchable.casefold()
    phrase = query.strip().casefold()
    score = 0.0
    if phrase and phrase in folded:
        score += 8.0
        if folded.startswith(phrase):
            score += 2.0
    matches = sum(1 for token in tokens if token in folded)
    if matches:
        score += matches + (matches / max(1, len(tokens))) * 3.0
    return score


def _parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utf8_prefix(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _bounded_snippet(body: str, tokens: Sequence[str], max_bytes: int) -> str:
    """Return a Unicode-safe snippet capped by UTF-8 bytes, not characters."""
    normalized = " ".join(str(body or "").split())
    if len(normalized.encode("utf-8")) <= max_bytes:
        return normalized
    folded = normalized.casefold()
    positions = [folded.find(token) for token in tokens if folded.find(token) >= 0]
    center = min(positions) if positions else 0
    # A character window is only a candidate.  The final UTF-8 prefix below
    # is the authoritative bound and handles multi-byte Bulgarian safely.
    start = max(0, center - max_bytes // 4)
    end = min(len(normalized), start + max_bytes)
    prefix = "…" if start else ""
    suffix = "…" if end < len(normalized) else ""
    marker_bytes = len((prefix + suffix).encode("utf-8"))
    snippet = _utf8_prefix(
        normalized[start:end].strip(), max(1, max_bytes - marker_bytes)
    ).rstrip()
    return prefix + snippet + suffix


def _metadata_boost(
    item: dict[str, Any],
    *,
    target_role: str,
    target_session_id: str,
    active_topics: set[str],
    unresolved: set[str],
    now: datetime,
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    recipients = set(item.get("recipients") or [])
    if target_role and target_role in recipients:
        score += 0.008
        reasons.append("direct_role")
    if target_session_id and target_session_id in recipients:
        score += 0.009
        reasons.append("direct_session")
    priority_boost = {"H": 0.006, "M": 0.003, "L": 0.001}.get(
        str(item.get("priority") or ""), 0.0
    )
    score += priority_boost
    if priority_boost:
        reasons.append(f"priority_{str(item.get('priority')).lower()}")
    kind_boost = {
        "Q": 0.005,
        "DECISION": 0.004,
        "PING": 0.003,
        "STATUS": 0.001,
        "A": 0.0005,
    }.get(str(item.get("kind") or ""), 0.0)
    score += kind_boost
    if kind_boost:
        reasons.append(f"kind_{str(item.get('kind')).lower()}")
    if item.get("msg_id") in unresolved:
        score += 0.006
        reasons.append("unresolved_question")
    if item.get("topic_id") in active_topics:
        score += 0.002
        reasons.append("active_topic")
    parsed = _parse_ts(str(item.get("ts") or ""))
    if parsed is not None:
        age_days = max(0.0, (now - parsed).total_seconds() / 86400.0)
        recency = 0.006 * math.exp(-age_days / 3.0)
        score += recency
        if recency >= 0.001:
            reasons.append("recent")
        if age_days > 14:
            score -= 0.002
            reasons.append("stale_penalty")
    body_chars = len(str(item.get("body") or ""))
    if body_chars > 1600:
        penalty = min(0.004, (body_chars - 1600) / 1_500_000)
        score -= penalty
        reasons.append("length_penalty")
    return score, reasons


def _search_debate_context_impl(
    conn: sqlite3.Connection,
    *,
    query: str,
    topic_ids: Sequence[str] = (),
    candidate_msg_ids: Sequence[str] = (),
    target_role: str = "",
    target_session_id: str = "",
    limit: int = 10,
    per_path_limit: int = 100,
    snippet_bytes: int = DEFAULT_SNIPPET_BYTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Search, fuse, and rank debate messages from one memory database.

    ``candidate_msg_ids`` lets inbox adapters rank only messages already proven
    addressed/unread by the authoritative signal DAO.  An empty candidate list
    means the search may inspect all messages in the selected topic(s).
    """
    effective_limit = max(1, min(int(limit or 1), 100))
    effective_path_limit = max(effective_limit, min(int(per_path_limit or 1), 500))
    effective_snippet = max(120, min(int(snippet_bytes or 120), 1200))
    clean_topics = _dedupe(topic_ids)
    clean_candidates = _dedupe(candidate_msg_ids)
    tokens = query_tokens(query)

    fts_rows = _fts_path(
        conn,
        tokens=tokens,
        topic_ids=clean_topics,
        candidate_msg_ids=clean_candidates,
        limit=effective_path_limit,
    )
    literal_pool = _literal_candidates(
        conn,
        topic_ids=clean_topics,
        candidate_msg_ids=clean_candidates,
        scan_limit=min(2000, max(200, effective_path_limit * 20)),
    )
    literal_scored = [
        (item, _literal_score(item, query, tokens))
        for item in literal_pool
    ]
    literal_scored = [pair for pair in literal_scored if pair[1] > 0]
    literal_scored = sorted(
        literal_scored,
        key=lambda pair: (
            pair[1],
            str(pair[0].get("ts") or ""),
            str(pair[0].get("msg_id") or ""),
        ),
        reverse=True,
    )[:effective_path_limit]

    data: dict[str, dict[str, Any]] = {}
    fused: dict[str, float] = {}
    source_ranks: dict[str, dict[str, int]] = {}
    for source, rows, weight in (
        ("fts_bm25", fts_rows, 1.0),
        ("literal_metadata", [item for item, _ in literal_scored], 0.9),
    ):
        for rank, item in enumerate(rows, start=1):
            msg_id = str(item["msg_id"])
            data[msg_id] = item
            fused[msg_id] = fused.get(msg_id, 0.0) + weight / (RRF_K + rank)
            source_ranks.setdefault(msg_id, {})[source] = rank

    if not data:
        return {
            "query": query,
            "topic_ids": clean_topics,
            "candidate_count": len(clean_candidates),
            "paths": {"fts_bm25": 0, "literal_metadata": 0},
            "merge": "weighted_rrf",
            "count": 0,
            "results": [],
        }

    ids = list(data)
    placeholders = ",".join("?" for _ in ids)
    unresolved = {
        str(row["msg_id"])
        for row in conn.execute(
            "SELECT q.msg_id FROM debate_messages q"
            f" WHERE q.msg_id IN ({placeholders}) AND q.kind='Q'"
            " AND NOT EXISTS (SELECT 1 FROM debate_messages a"
            " WHERE a.topic_id=q.topic_id AND a.reply_to=q.msg_id AND a.kind='A')",
            ids,
        ).fetchall()
    }
    active_topics = {
        str(row["topic_id"])
        for row in conn.execute(
            "SELECT topic_id FROM debates WHERE state IN ('INIT','ACTIVE')"
        ).fetchall()
    }
    if now is None:
        # Anchor recency to the newest candidate, not wall-clock time.  This
        # makes identical query + identical DB state byte-deterministic while
        # still preferring newer messages inside the selected ledger slice.
        parsed_times = [
            parsed
            for item in data.values()
            if (parsed := _parse_ts(str(item.get("ts") or ""))) is not None
        ]
        now_utc = max(parsed_times, default=datetime(1970, 1, 1, tzinfo=timezone.utc))
    else:
        now_utc = now.astimezone(timezone.utc)
    ranked: list[dict[str, Any]] = []
    for msg_id, item in data.items():
        metadata_score, reasons = _metadata_boost(
            item,
            target_role=target_role,
            target_session_id=target_session_id,
            active_topics=active_topics,
            unresolved=unresolved,
            now=now_utc,
        )
        body = str(item.pop("body", "") or "")
        ranked.append(
            {
                **item,
                "body_chars": len(body),
                "body_bytes": len(body.encode("utf-8")),
                "snippet": _bounded_snippet(body, tokens, effective_snippet),
                "source_ranks": source_ranks[msg_id],
                "rrf_score": round(fused[msg_id], 8),
                "metadata_score": round(metadata_score, 8),
                "score": round(fused[msg_id] + metadata_score, 8),
                "rank_reasons": reasons,
            }
        )
    ranked.sort(
        key=lambda item: (
            float(item["score"]),
            str(item.get("ts") or ""),
            str(item.get("msg_id") or ""),
        ),
        reverse=True,
    )
    ranked = ranked[:effective_limit]
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
    return {
        "query": query,
        "topic_ids": clean_topics,
        "candidate_count": len(clean_candidates),
        "paths": {
            "fts_bm25": len(fts_rows),
            "literal_metadata": len(literal_scored),
        },
        "merge": "weighted_rrf",
        "count": len(ranked),
        "results": ranked,
    }


def search_debate_context(
    conn: sqlite3.Connection,
    *,
    query: str,
    topic_ids: Sequence[str] = (),
    candidate_msg_ids: Sequence[str] = (),
    target_role: str = "",
    target_session_id: str = "",
    limit: int = 10,
    per_path_limit: int = 100,
    snippet_bytes: int = DEFAULT_SNIPPET_BYTES,
    max_query_ms: int = DEFAULT_MAX_QUERY_MS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run deterministic dual-path retrieval under a hard SQLite deadline.

    Weighted RRF uses ``score = sum(weight / (RRF_K + rank))`` with
    ``RRF_K=60``, FTS weight 1.0, and literal/metadata weight 0.9.  Final
    ordering is score DESC, timestamp DESC, msg_id DESC.  The progress
    handler aborts SQLite work once ``max_query_ms`` elapses; callers receive
    an OperationalError instead of an unbounded prompt-time scan.
    """
    budget_ms = max(25, min(int(max_query_ms or 25), 5000))
    deadline = time.monotonic() + budget_ms / 1000.0

    def _deadline_reached() -> int:
        return int(time.monotonic() >= deadline)

    conn.set_progress_handler(_deadline_reached, 1000)
    try:
        return _search_debate_context_impl(
            conn,
            query=query,
            topic_ids=topic_ids,
            candidate_msg_ids=candidate_msg_ids,
            target_role=target_role,
            target_session_id=target_session_id,
            limit=limit,
            per_path_limit=per_path_limit,
            snippet_bytes=snippet_bytes,
            now=now,
        )
    finally:
        conn.set_progress_handler(None, 0)
