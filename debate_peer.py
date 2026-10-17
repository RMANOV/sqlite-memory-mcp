"""Opt-in peer discussion and immutable, equal-weight local ballots.

These DAOs never touch the legacy debate message/delivery tables. Callers own
transactions and must propagate failures out of the transaction context so the
entire operation rolls back. Writes reserve SQLite's writer before validation
reads; reads require a caller-owned transaction for a consistent snapshot.
Session IDs are cooperative local attribution, not authenticated principals.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

PROTOCOL_VERSION = "peer/v1"
MIN_MEMBERS = 2
MAX_MEMBERS = 32
MAX_BODY_LENGTH = 16_384
MAX_TITLE_LENGTH = 256
MAX_READ_LIMIT = 200
TOPIC_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MEMBER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
CHOICES = ("approve", "reject", "abstain")
TERMINAL_STATES = ("ACCEPTED", "REJECTED", "INCONCLUSIVE")
LIMITATIONS = (
    "Cooperative local session attribution; not cryptographic authentication.",
    "Equal logical members do not prove independent people or models.",
    "Decisions do not execute actions or imply external/human approval.",
    "No automatic delivery, session launch, read receipt, or deadline.",
)

# Execute each statement independently inside schema.init_db's transaction.
# executescript() would implicitly commit and must not be used by callers.
SCHEMA_SQL = (
    """CREATE TABLE IF NOT EXISTS debate_peer_topics (
        topic_id TEXT PRIMARY KEY,
        protocol_version TEXT NOT NULL CHECK (protocol_version = 'peer/v1'),
        title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 256),
        members_json TEXT NOT NULL CHECK (json_valid(members_json)),
        config_hash TEXT NOT NULL CHECK (length(config_hash) = 64),
        creator_member_id TEXT NOT NULL,
        creator_session_id TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS debate_peer_posts (
        post_id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL REFERENCES debate_peer_topics(topic_id),
        member_id TEXT NOT NULL,
        author_session_id TEXT NOT NULL,
        body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 16384),
        body_hash TEXT NOT NULL CHECK (length(body_hash) = 64),
        created_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_peer_posts_topic_time
        ON debate_peer_posts(topic_id, created_at, post_id)""",
    """CREATE TABLE IF NOT EXISTS debate_peer_proposals (
        proposal_id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL REFERENCES debate_peer_topics(topic_id),
        member_id TEXT NOT NULL,
        author_session_id TEXT NOT NULL,
        body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 16384),
        body_hash TEXT NOT NULL CHECK (length(body_hash) = 64),
        config_hash TEXT NOT NULL CHECK (length(config_hash) = 64),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version = 1),
        state TEXT NOT NULL DEFAULT 'OPEN'
            CHECK (state IN ('OPEN','ACCEPTED','REJECTED','INCONCLUSIVE')),
        result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
        created_at TEXT NOT NULL,
        closed_at TEXT,
        UNIQUE (proposal_id, topic_id, body_hash),
        CHECK ((state = 'OPEN' AND result_json IS NULL AND closed_at IS NULL)
            OR (state <> 'OPEN' AND result_json IS NOT NULL AND closed_at IS NOT NULL))
    )""",
    """CREATE INDEX IF NOT EXISTS idx_peer_proposals_topic_time
        ON debate_peer_proposals(topic_id, created_at, proposal_id)""",
    """CREATE TABLE IF NOT EXISTS debate_peer_ballots (
        proposal_id TEXT NOT NULL,
        topic_id TEXT NOT NULL,
        member_id TEXT NOT NULL,
        author_session_id TEXT NOT NULL,
        choice TEXT NOT NULL CHECK (choice IN ('approve','reject','abstain')),
        proposal_hash TEXT NOT NULL CHECK (length(proposal_hash) = 64),
        created_at TEXT NOT NULL,
        PRIMARY KEY (proposal_id, member_id),
        FOREIGN KEY (proposal_id, topic_id, proposal_hash)
            REFERENCES debate_peer_proposals(proposal_id, topic_id, body_hash)
    )""",
    """CREATE TRIGGER IF NOT EXISTS peer_topic_no_replace
        BEFORE INSERT ON debate_peer_topics
        WHEN EXISTS (SELECT 1 FROM debate_peer_topics WHERE NEW.topic_id = topic_id) BEGIN
            SELECT RAISE(ABORT, 'peer topic identity cannot be replaced');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_post_no_replace
        BEFORE INSERT ON debate_peer_posts
        WHEN EXISTS (SELECT 1 FROM debate_peer_posts WHERE NEW.post_id = post_id) BEGIN
            SELECT RAISE(ABORT, 'peer post identity cannot be replaced');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_proposal_no_replace
        BEFORE INSERT ON debate_peer_proposals
        WHEN EXISTS (SELECT 1 FROM debate_peer_proposals WHERE NEW.proposal_id = proposal_id) BEGIN
            SELECT RAISE(ABORT, 'peer proposal identity cannot be replaced');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_ballot_no_replace
        BEFORE INSERT ON debate_peer_ballots
        WHEN EXISTS (SELECT 1 FROM debate_peer_ballots WHERE NEW.proposal_id = proposal_id AND NEW.member_id = member_id) BEGIN
            SELECT RAISE(ABORT, 'peer ballot identity cannot be replaced');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_topic_immutable_update
        BEFORE UPDATE ON debate_peer_topics BEGIN
            SELECT RAISE(ABORT, 'peer topic/config is immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_topic_immutable_delete
        BEFORE DELETE ON debate_peer_topics BEGIN
            SELECT RAISE(ABORT, 'peer topic/config is immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_post_immutable_update
        BEFORE UPDATE ON debate_peer_posts BEGIN
            SELECT RAISE(ABORT, 'peer post is immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_post_immutable_delete
        BEFORE DELETE ON debate_peer_posts BEGIN
            SELECT RAISE(ABORT, 'peer post is immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_proposal_immutable_update
        BEFORE UPDATE ON debate_peer_proposals
        WHEN OLD.state <> 'OPEN'
          OR NEW.proposal_id IS NOT OLD.proposal_id
          OR NEW.topic_id IS NOT OLD.topic_id
          OR NEW.member_id IS NOT OLD.member_id
          OR NEW.author_session_id IS NOT OLD.author_session_id
          OR NEW.body IS NOT OLD.body
          OR NEW.body_hash IS NOT OLD.body_hash
          OR NEW.config_hash IS NOT OLD.config_hash
          OR NEW.version IS NOT OLD.version
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.state NOT IN ('ACCEPTED','REJECTED','INCONCLUSIVE')
        BEGIN
            SELECT RAISE(ABORT, 'peer proposal payload/result is immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_proposal_immutable_delete
        BEFORE DELETE ON debate_peer_proposals BEGIN
            SELECT RAISE(ABORT, 'peer proposal is immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_ballot_immutable_update
        BEFORE UPDATE ON debate_peer_ballots BEGIN
            SELECT RAISE(ABORT, 'peer ballot is immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_ballot_immutable_delete
        BEFORE DELETE ON debate_peer_ballots BEGIN
            SELECT RAISE(ABORT, 'peer ballot is immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_ballot_open_proposal
        BEFORE INSERT ON debate_peer_ballots
        WHEN NOT EXISTS (
            SELECT 1 FROM debate_peer_proposals p
            WHERE p.proposal_id = NEW.proposal_id
              AND p.topic_id = NEW.topic_id
              AND p.body_hash = NEW.proposal_hash AND p.state = 'OPEN'
        ) BEGIN
            SELECT RAISE(ABORT, 'peer proposal is absent or terminal');
        END""",
    """CREATE TRIGGER IF NOT EXISTS peer_ballot_frozen_member
        BEFORE INSERT ON debate_peer_ballots
        WHEN NOT EXISTS (
            SELECT 1 FROM debate_peer_topics t, json_each(t.members_json) m
            WHERE t.topic_id = NEW.topic_id
              AND json_extract(m.value, '$.member_id') = NEW.member_id
              AND json_extract(m.value, '$.session_id') = NEW.author_session_id
        ) BEGIN
            SELECT RAISE(ABORT, 'peer ballot author is not a frozen member/session');
        END""",
)


class PeerError(ValueError):
    """Typed rejection; a failed write requires caller transaction rollback."""

    def __init__(
        self, message: str, *, error_type: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.details = details or {}


def _reject(error_type: str, message: str, **details: Any) -> None:
    raise PeerError(message, error_type=error_type, details=details)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> dict | None:
    cursor = conn.execute(sql, args)
    row = cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description), row)) if row else None


def _all(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    cursor = conn.execute(sql, args)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _transaction(conn: sqlite3.Connection, *, write: bool = False) -> None:
    if not conn.in_transaction:
        _reject("TRANSACTION_REQUIRED", "Caller must own a transaction")
    if write:
        # SQLite has no public Python query for BEGIN mode. This no-row write
        # obtains a RESERVED writer lock before reads, even for DEFERRED callers.
        # No row trigger runs and total_changes remains unchanged.
        conn.execute("UPDATE debate_peer_topics SET title=title WHERE 0")


def _identifier(value: Any, name: str, pattern: re.Pattern = _ID_RE) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _reject("INVALID_INPUT", f"Invalid {name}", field=name)
    return value


def _text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _reject("INVALID_INPUT", f"Invalid {name}", field=name, maximum=maximum)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _reject("INVALID_INPUT", f"Invalid UTF-8 {name}", field=name)
    return value


def _members(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not MIN_MEMBERS <= len(value) <= MAX_MEMBERS:
        _reject("INVALID_MEMBERS", "Peer topics require 2–32 members")
    normalized = []
    ids: set[str] = set()
    sessions: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"member_id", "session_id"}:
            _reject("INVALID_MEMBERS", "Members require exactly member_id and session_id")
        member = _identifier(entry["member_id"], "member_id", _MEMBER_RE)
        session = _identifier(entry["session_id"], "session_id")
        if member in ids or session in sessions:
            _reject("INVALID_MEMBERS", "Duplicate member or session identity")
        ids.add(member)
        sessions.add(session)
        normalized.append({"member_id": member, "session_id": session})
    return sorted(normalized, key=lambda entry: entry["member_id"])


def topic_exists(conn: sqlite3.Connection, topic_id: str) -> bool:
    """Persisted namespace reservation, independent of feature configuration."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='debate_peer_topics'"
    ).fetchone()
    return bool(table and conn.execute(
        "SELECT 1 FROM debate_peer_topics WHERE topic_id=?", (topic_id,)
    ).fetchone())


def _topic(conn: sqlite3.Connection, topic_id: str) -> dict:
    _identifier(topic_id, "topic_id", TOPIC_RE)
    row = _one(conn, "SELECT * FROM debate_peer_topics WHERE topic_id=?", (topic_id,))
    if row is None:
        _reject("PEER_TOPIC_NOT_FOUND", "Unknown peer topic", topic_id=topic_id)
    row["members"] = json.loads(row.pop("members_json"))
    return row


def _authorize(topic: dict, member_id: str, author_session_id: str) -> None:
    _identifier(member_id, "member_id", _MEMBER_RE)
    _identifier(author_session_id, "author_session_id")
    if not any(
        entry["member_id"] == member_id and entry["session_id"] == author_session_id
        for entry in topic["members"]
    ):
        _reject("PEER_AUTHOR_UNAUTHORIZED", "Author does not own the frozen member")


def _public_topic(topic: dict) -> dict:
    return {**topic, "majority_threshold": len(topic["members"]) // 2 + 1,
            "limitations": list(LIMITATIONS)}


def create_topic(
    conn: sqlite3.Connection, *, topic_id: str, title: str,
    members: list[dict[str, str]], creator_member_id: str, author_session_id: str,
) -> dict:
    """Create a reserved peer namespace, or replay its exact frozen config."""
    _transaction(conn, write=True)
    _identifier(topic_id, "topic_id", TOPIC_RE)
    title = _text(title, "title", MAX_TITLE_LENGTH)
    roster = _members(members)
    candidate = {"members": roster}
    _authorize(candidate, creator_member_id, author_session_id)
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='debates'"
    ).fetchone() and conn.execute(
        "SELECT 1 FROM debates WHERE topic_id=?", (topic_id,)
    ).fetchone():
        _reject("TOPIC_NAMESPACE_CONFLICT", "Topic ID belongs to a legacy debate")
    config_hash = _hash(_json({
        "protocol_version": PROTOCOL_VERSION, "topic_id": topic_id,
        "title": title, "members": roster,
    }))
    if topic_exists(conn, topic_id):
        existing = _topic(conn, topic_id)
        _authorize(existing, creator_member_id, author_session_id)
        if existing["config_hash"] != config_hash:
            _reject("PEER_CONFIG_CONFLICT", "Existing peer configuration is immutable")
        return {**_public_topic(existing), "replayed": True}
    conn.execute(
        "INSERT INTO debate_peer_topics "
        "(topic_id,protocol_version,title,members_json,config_hash,creator_member_id,"
        "creator_session_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (topic_id, PROTOCOL_VERSION, title, _json(roster), config_hash,
         creator_member_id, author_session_id, _now()),
    )
    return {**_public_topic(_topic(conn, topic_id)), "replayed": False}


def post(
    conn: sqlite3.Connection, *, topic_id: str, post_id: str, member_id: str,
    author_session_id: str, body: str,
) -> dict:
    """Append plain text without legacy message or delivery side effects."""
    _transaction(conn, write=True)
    topic = _topic(conn, topic_id)
    _authorize(topic, member_id, author_session_id)
    _identifier(post_id, "post_id")
    body = _text(body, "body", MAX_BODY_LENGTH)
    existing = _one(conn, "SELECT * FROM debate_peer_posts WHERE post_id=?", (post_id,))
    if existing is not None:
        if (existing["topic_id"], existing["member_id"], existing["author_session_id"],
            existing["body"]) != (topic_id, member_id, author_session_id, body):
            _reject("PEER_POST_CONFLICT", "Post ID already has different immutable content")
        return {**existing, "replayed": True}
    conn.execute(
        "INSERT INTO debate_peer_posts "
        "(post_id,topic_id,member_id,author_session_id,body,body_hash,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (post_id, topic_id, member_id, author_session_id, body, _hash(body), _now()),
    )
    return {**_one(conn, "SELECT * FROM debate_peer_posts WHERE post_id=?", (post_id,)),
            "replayed": False}


def _proposal(conn: sqlite3.Connection, topic_id: str, proposal_id: str) -> dict:
    _identifier(proposal_id, "proposal_id")
    row = _one(conn, "SELECT * FROM debate_peer_proposals WHERE proposal_id=? AND topic_id=?",
               (proposal_id, topic_id))
    if row is None:
        _reject("PEER_PROPOSAL_NOT_FOUND", "Unknown proposal in peer topic")
    return row


def _ballots(conn: sqlite3.Connection, proposal_id: str) -> list[dict]:
    return _all(conn, "SELECT * FROM debate_peer_ballots WHERE proposal_id=? ORDER BY member_id",
                (proposal_id,))


def _tally(topic: dict, ballots: list[dict]) -> dict:
    members = [entry["member_id"] for entry in topic["members"]]
    counts = {choice: 0 for choice in CHOICES}
    for ballot in ballots:
        counts[ballot["choice"]] += 1
    threshold = len(members) // 2 + 1
    if counts["approve"] >= threshold:
        state = "ACCEPTED"
    elif counts["reject"] >= threshold:
        state = "REJECTED"
    elif len(ballots) == len(members):
        state = "INCONCLUSIVE"
    else:
        state = "OPEN"
    voted = {row["member_id"] for row in ballots}
    return {"state": state, "eligible_count": len(members), "threshold": threshold,
            "counts": counts, "voted_count": len(ballots),
            "missing_members": [member for member in members if member not in voted]}


def _projection(conn: sqlite3.Connection, topic: dict, proposal: dict) -> dict:
    ballots = _ballots(conn, proposal["proposal_id"])
    result = dict(proposal)
    encoded = result.pop("result_json")
    result["result"] = json.loads(encoded) if encoded is not None else None
    result["tally"] = _tally(topic, ballots)
    result["ballots"] = ballots
    return result


def propose(
    conn: sqlite3.Connection, *, topic_id: str, proposal_id: str, member_id: str,
    author_session_id: str, body: str,
) -> dict:
    """Create an immutable proposal; the proposer does not implicitly vote."""
    _transaction(conn, write=True)
    topic = _topic(conn, topic_id)
    _authorize(topic, member_id, author_session_id)
    _identifier(proposal_id, "proposal_id")
    body = _text(body, "body", MAX_BODY_LENGTH)
    existing = _one(conn, "SELECT * FROM debate_peer_proposals WHERE proposal_id=?", (proposal_id,))
    if existing is not None:
        if (existing["topic_id"], existing["member_id"], existing["author_session_id"],
            existing["body"], existing["config_hash"]) != (
                topic_id, member_id, author_session_id, body, topic["config_hash"]):
            _reject("PEER_PROPOSAL_CONFLICT", "Proposal ID already has different content")
        return {**_projection(conn, topic, existing), "replayed": True}
    conn.execute(
        "INSERT INTO debate_peer_proposals "
        "(proposal_id,topic_id,member_id,author_session_id,body,body_hash,config_hash,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (proposal_id, topic_id, member_id, author_session_id, body, _hash(body),
         topic["config_hash"], _now()),
    )
    return {**_projection(conn, topic, _proposal(conn, topic_id, proposal_id)), "replayed": False}


def vote(
    conn: sqlite3.Connection, *, topic_id: str, proposal_id: str, member_id: str,
    author_session_id: str, expected_proposal_hash: str, choice: str,
) -> dict:
    """Cast one immutable ballot and finalize an absolute majority atomically."""
    _transaction(conn, write=True)
    topic = _topic(conn, topic_id)
    _authorize(topic, member_id, author_session_id)
    proposal = _proposal(conn, topic_id, proposal_id)
    if not isinstance(expected_proposal_hash, str) or not _HASH_RE.fullmatch(expected_proposal_hash):
        _reject("INVALID_INPUT", "Expected proposal hash must be lower-case SHA-256")
    if expected_proposal_hash != proposal["body_hash"]:
        _reject("PEER_PROPOSAL_HASH_MISMATCH", "Ballot does not identify the exact proposal body")
    if proposal["config_hash"] != topic["config_hash"]:
        _reject("PEER_ELECTORATE_MISMATCH", "Proposal electorate differs from frozen topic")
    if not isinstance(choice, str) or choice not in CHOICES:
        _reject("INVALID_INPUT", "Choice must be approve, reject, or abstain")
    existing = _one(conn,
        "SELECT * FROM debate_peer_ballots WHERE proposal_id=? AND member_id=?",
        (proposal_id, member_id))
    if existing is not None:
        if (existing["choice"], existing["author_session_id"], existing["proposal_hash"]) != (
            choice, author_session_id, expected_proposal_hash):
            _reject("PEER_BALLOT_CONFLICT", "An immutable ballot already occupies this member slot")
        return {**_projection(conn, topic, proposal), "replayed": True}
    if proposal["state"] != "OPEN":
        _reject("PEER_PROPOSAL_CLOSED", "Proposal already has a terminal result")
    conn.execute(
        "INSERT INTO debate_peer_ballots "
        "(proposal_id,topic_id,member_id,author_session_id,choice,proposal_hash,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (proposal_id, topic_id, member_id, author_session_id, choice,
         expected_proposal_hash, _now()),
    )
    tally = _tally(topic, _ballots(conn, proposal_id))
    if tally["state"] != "OPEN":
        result = {**tally, "proposal_hash": proposal["body_hash"],
                  "config_hash": topic["config_hash"], "decision_only": True}
        updated = conn.execute(
            "UPDATE debate_peer_proposals SET state=?,result_json=?,closed_at=? "
            "WHERE proposal_id=? AND state='OPEN'",
            (tally["state"], _json(result), _now(), proposal_id),
        )
        if updated.rowcount != 1:
            _reject("PEER_FINALIZATION_CONFLICT", "Proposal could not be finalized exactly once")
    return {**_projection(conn, topic, _proposal(conn, topic_id, proposal_id)), "replayed": False}


def read_state(
    conn: sqlite3.Connection, *, topic_id: str, limit: int = 50,
    proposal_id: str | None = None,
) -> dict:
    """Pure SELECT snapshot with bounded recent posts/proposals and full ballots."""
    _transaction(conn)
    topic = _topic(conn, topic_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_READ_LIMIT:
        _reject("INVALID_INPUT", "Read limit must be an integer between 1 and 200")
    posts = _all(conn, "SELECT * FROM debate_peer_posts WHERE topic_id=? "
                 "ORDER BY created_at DESC,post_id DESC LIMIT ?", (topic_id, limit))
    posts.reverse()
    if proposal_id is not None:
        proposals = [_proposal(conn, topic_id, proposal_id)]
    else:
        proposals = _all(conn, "SELECT * FROM debate_peer_proposals WHERE topic_id=? "
                          "ORDER BY created_at DESC,proposal_id DESC LIMIT ?", (topic_id, limit))
        proposals.reverse()
    counts = {
        "posts": conn.execute("SELECT COUNT(*) FROM debate_peer_posts WHERE topic_id=?", (topic_id,)).fetchone()[0],
        "proposals": conn.execute("SELECT COUNT(*) FROM debate_peer_proposals WHERE topic_id=?", (topic_id,)).fetchone()[0],
    }
    return {"topic": _public_topic(topic), "posts": posts,
            "proposals": [_projection(conn, topic, row) for row in proposals],
            "totals": counts, "limit": limit,
            "truncated": {"posts": counts["posts"] > len(posts),
                          "proposals": proposal_id is None and counts["proposals"] > len(proposals)}}
