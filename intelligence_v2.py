"""Intelligence v2 — Context state machine, signal detection, and enrichment policy.

Core module implementing human-sovereign knowledge management:
- Signal phrase detection (ENRICH_OK, NO_ENRICH, WAIT_HUMAN, FREEZE_CONTEXT)
- Context chunk lifecycle state machine
- Uncertainty scoring and AWAITING_HUMAN block generation
- Enrichment run audit logging
- Delta-aware reprocessing (skip on same source_hash)
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from db_utils import (
    json_loads,
    now_iso,
)

_log = logging.getLogger("intelligence-v2")

# ── Configuration ────────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent / "intelligence_config.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "default_enrich_policy": "manual",
    "respect_signal_phrases": True,
    "skip_same_unresolved_source": True,
    "max_questions_per_chunk": 5,
    "auto_promote_scopes": ["memory"],
    "human_required_scopes": ["mapping", "validation", "bridge", "export"],
    "context_pack_token_budget_default": 4000,
    "conflict_detection_enabled": False,
    "query_expansion_enabled": False,
    "advanced_context_enabled": False,
    "advanced_context_submodular_enabled": True,
    "advanced_context_max_seed_entities": 8,
    "advanced_context_max_related_entities": 12,
    "advanced_context_max_expansion_keywords": 24,
}


def load_config() -> dict[str, Any]:
    """Load intelligence config with graceful fallback to safe defaults."""
    try:
        return {
            **_DEFAULT_CONFIG,
            **json_loads(_CONFIG_PATH.read_text(encoding="utf-8")),
        }
    except (FileNotFoundError, ValueError, OSError):
        _log.warning("intelligence_config.json missing or invalid — using defaults")
        return dict(_DEFAULT_CONFIG)


# ── Constants ────────────────────────────────────────────────────────────

# Valid context chunk states (lifecycle)
CHUNK_STATES = (
    "no_enrich",  # default — no enrichment allowed
    "enrichable",  # opt-in signal received, ready for AI processing
    "uncertain",  # AI parsed but couldn't extract confident claims
    "awaiting_human",  # questions generated, waiting for human answer
    "frozen",  # FREEZE_CONTEXT — no mutation allowed
    "stale",  # newer evidence available, needs re-assessment
    "archived",  # retired, kept for audit trail
)

# Valid transitions: (from_state, to_state)
_VALID_TRANSITIONS = frozenset(
    {
        ("no_enrich", "enrichable"),  # assess_context can unlock no_enrich chunks
        ("no_enrich", "frozen"),
        ("no_enrich", "stale"),
        ("no_enrich", "archived"),
        ("enrichable", "uncertain"),
        ("enrichable", "awaiting_human"),
        ("enrichable", "frozen"),
        ("enrichable", "stale"),
        ("enrichable", "archived"),
        ("uncertain", "awaiting_human"),
        ("uncertain", "enrichable"),  # after human answers questions
        ("uncertain", "frozen"),
        ("uncertain", "stale"),
        ("awaiting_human", "enrichable"),  # human answered
        ("awaiting_human", "frozen"),
        ("awaiting_human", "stale"),
        ("frozen", "enrichable"),  # explicit unfreeze
        ("frozen", "archived"),
        ("stale", "enrichable"),
        ("stale", "archived"),
        ("enrichable", "no_enrich"),  # NO_ENRICH signal revokes enrichment
        ("uncertain", "no_enrich"),  # NO_ENRICH signal revokes enrichment
        ("awaiting_human", "no_enrich"),  # NO_ENRICH signal revokes enrichment
    }
)

# Signal phrase patterns (case-insensitive)
_SIGNAL_PATTERNS = {
    "ENRICH_OK": re.compile(r"\bENRICH_OK\b", re.IGNORECASE),
    "NO_ENRICH": re.compile(r"\bNO_ENRICH\b", re.IGNORECASE),
    "WAIT_HUMAN": re.compile(r"\bWAIT_HUMAN\b", re.IGNORECASE),
    "FREEZE_CONTEXT": re.compile(r"\bFREEZE_CONTEXT\b", re.IGNORECASE),
}

# Source types for context chunks
SOURCE_TYPES = (
    "note",
    "task_desc",
    "session_log",
    "bridge_event",
    "entity_obs",
    "manual",
)

# Annotation types
ANNOTATION_TYPES = ("signal", "awaiting_human", "note", "resolution", "freeze")

# Author types
AUTHOR_TYPES = ("human", "ai", "system")

# Question types for structured clarification
QUESTION_TYPES = ("scope", "semantics", "owner", "time", "action", "downstream_use")

# Enrichment run statuses
RUN_STATUSES = ("success", "skipped", "blocked", "failed")


# ── Helpers ──────────────────────────────────────────────────────────────


def compute_source_hash(text: str) -> str:
    """SHA-256 hash of text content for delta detection."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_signals(text: str) -> list[str]:
    """Scan text for signal phrases. Returns list of detected signal names."""
    return [name for name, pattern in _SIGNAL_PATTERNS.items() if pattern.search(text)]


def is_valid_transition(from_state: str, to_state: str) -> bool:
    """Check if state transition is allowed."""
    return (from_state, to_state) in _VALID_TRANSITIONS


def _new_id() -> str:
    """Generate a UUID for new records."""
    return uuid.uuid4().hex


# ── Uncertainty Scoring ──────────────────────────────────────────────────

# Markers that indicate ambiguity in text
_AMBIGUITY_MARKERS = re.compile(
    r"\b(може би|вероятно|не е ясно|perhaps|maybe|unclear|TODO|TBD|FIXME|"
    r"not sure|possibly|likely|depends on|to be determined)\b",
    re.IGNORECASE,
)

# Completeness indicators (presence lowers uncertainty)
_COMPLETENESS_INDICATORS = re.compile(
    r"\b(confirmed|verified|agreed|approved|решено|потвърдено|одобрено|"
    r"final|done|complete|accepted)\b",
    re.IGNORECASE,
)


def compute_uncertainty(text: str) -> float:
    """Score uncertainty of a text chunk (0.0 = certain, 1.0 = fully uncertain).

    Heuristic based on:
    - Ambiguity marker density
    - Completeness indicator density
    - Text length (very short text = less informative = higher uncertainty)
    """
    words = text.split()
    word_count = max(len(words), 1)

    ambiguity_count = len(_AMBIGUITY_MARKERS.findall(text))
    completeness_count = len(_COMPLETENESS_INDICATORS.findall(text))

    # Base uncertainty from ambiguity density
    ambiguity_score = min(ambiguity_count / word_count * 10.0, 1.0)

    # Completeness reduces uncertainty
    completeness_bonus = min(completeness_count / word_count * 10.0, 0.5)

    # Very short text (<20 words) adds uncertainty
    brevity_penalty = max(0.0, (20 - word_count) / 20.0) * 0.3

    score = ambiguity_score - completeness_bonus + brevity_penalty
    return max(0.0, min(1.0, score))


# ── Materiality Scoring ──────────────────────────────────────────────────

# Keywords indicating high-materiality content
_MATERIALITY_KEYWORDS = {
    "critical": 0.9,
    "mapping": 0.8,
    "validation": 0.8,
    "bridge": 0.7,
    "export": 0.7,
    "accounting": 0.9,
    "GAAP": 0.9,
    "IFRS": 0.9,
    "balance": 0.8,
    "financial": 0.8,
    "audit": 0.8,
    "compliance": 0.8,
    "error": 0.6,
    "bug": 0.5,
    "fix": 0.4,
    "config": 0.4,
    "критично": 0.9,
    "грешка": 0.6,
    "счетоводство": 0.9,
    "баланс": 0.8,
}


def compute_materiality(text: str, scope_hint: str | None = None) -> float:
    """Score materiality of a text chunk (0.0 = trivial, 1.0 = critical).

    High materiality = mapping correctness, accounting invariants, bridge workflow.
    """
    text_lower = text.lower()
    scores = [
        weight
        for keyword, weight in _MATERIALITY_KEYWORDS.items()
        if keyword.lower() in text_lower
    ]

    # Scope hint boosts materiality
    scope_boost = 0.0
    if scope_hint in ("mapping", "validation", "bridge", "export"):
        scope_boost = 0.3

    keyword_score = max(scores) if scores else 0.1
    return min(1.0, keyword_score + scope_boost)


# ── Audit Trail ──────────────────────────────────────────────────────────


def log_enrichment_run(
    conn: sqlite3.Connection,
    tool_name: str,
    result_status: str,
    input_signature: str,
    chunk_id: str | None = None,
    session_id: str | None = None,
    reason_code: str | None = None,
    started_at: str | None = None,
) -> str:
    """Record an enrichment run in the audit trail. Returns run_id."""
    run_id = _new_id()
    finished = now_iso()
    conn.execute(
        "INSERT INTO enrichment_runs "
        "(run_id, tool_name, chunk_id, session_id, result_status, reason_code, "
        "input_signature, started_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            tool_name,
            chunk_id,
            session_id,
            result_status,
            reason_code,
            input_signature,
            started_at or finished,
            finished,
        ),
    )
    return run_id


# ── Core: Assess Context ─────────────────────────────────────────────────


def assess_context(
    conn: sqlite3.Connection,
    chunk_ref: str,
    session_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Classify context chunk, detect signals, determine state transition.

    Returns dict with: chunk_id, state, policy, materiality, uncertainty,
    should_skip, skip_reason, signals_detected, annotations_created.
    """
    config = load_config()
    started = now_iso()

    if not config["enabled"]:
        return {"status": "disabled", "message": "Intelligence v2 is disabled"}

    # Find or create chunk
    row = conn.execute(
        "SELECT * FROM context_chunks WHERE chunk_id = ?", (chunk_ref,)
    ).fetchone()

    if row is None:
        # chunk_ref might be an entity_id or task reference — create new chunk
        return {
            "error": f"Chunk '{chunk_ref}' not found. Use ingest to create chunks first."
        }

    chunk_id = row["chunk_id"]
    current_state = row["state"]
    body = row["body"]
    source_hash = row["source_hash"]

    # Frozen → blocked
    if current_state == "frozen" and not force:
        log_enrichment_run(
            conn,
            "assess_context",
            "blocked",
            input_signature=source_hash,
            chunk_id=chunk_id,
            session_id=session_id,
            reason_code="frozen",
            started_at=started,
        )
        return {
            "chunk_id": chunk_id,
            "state": "frozen",
            "should_skip": True,
            "skip_reason": "chunk is frozen — no mutation allowed",
        }

    # Detect signal phrases in body
    signals = detect_signals(body) if config["respect_signal_phrases"] else []

    # Determine target state based on signals
    new_state = current_state
    annotations_created = []

    if "FREEZE_CONTEXT" in signals:
        if is_valid_transition(current_state, "frozen"):
            new_state = "frozen"
            annotations_created.append(
                _create_annotation(
                    conn,
                    chunk_id,
                    "system",
                    "freeze",
                    "FREEZE_CONTEXT signal detected",
                    source_hash,
                )
            )

    elif "NO_ENRICH" in signals:
        if is_valid_transition(current_state, "no_enrich"):
            new_state = "no_enrich"
            annotations_created.append(
                _create_annotation(
                    conn,
                    chunk_id,
                    "system",
                    "signal",
                    "NO_ENRICH signal detected — enrichment revoked",
                    source_hash,
                )
            )

    elif "WAIT_HUMAN" in signals:
        if is_valid_transition(current_state, "awaiting_human"):
            new_state = "awaiting_human"
            annotations_created.append(
                _create_annotation(
                    conn,
                    chunk_id,
                    "system",
                    "signal",
                    "WAIT_HUMAN signal detected — waiting for human input",
                    source_hash,
                )
            )

    elif "ENRICH_OK" in signals:
        if is_valid_transition(current_state, "enrichable"):
            new_state = "enrichable"
            annotations_created.append(
                _create_annotation(
                    conn,
                    chunk_id,
                    "system",
                    "signal",
                    "ENRICH_OK signal detected — chunk is now enrichable",
                    source_hash,
                )
            )

    else:
        # No explicit signals — unlock no_enrich chunks by default
        if current_state == "no_enrich" and is_valid_transition(
            "no_enrich", "enrichable"
        ):
            new_state = "enrichable"

    # Skip logic: awaiting_human + same source_hash → skip
    # But bypass skip if signals would change the entity's state
    if (
        current_state == "awaiting_human"
        and new_state == current_state
        and config["skip_same_unresolved_source"]
        and not force
    ):
        # Check if source_hash is unchanged since last AI attempt
        last_hash = conn.execute(
            "SELECT source_hash_seen FROM context_annotations "
            "WHERE chunk_id = ? AND annotation_type = 'awaiting_human' "
            "ORDER BY created_at DESC LIMIT 1",
            (chunk_id,),
        ).fetchone()
        if last_hash and last_hash["source_hash_seen"] == source_hash:
            log_enrichment_run(
                conn,
                "assess_context",
                "skipped",
                source_hash,
                chunk_id=chunk_id,
                session_id=session_id,
                reason_code="same_unresolved_source",
                started_at=started,
            )
            return {
                "chunk_id": chunk_id,
                "state": current_state,
                "should_skip": True,
                "skip_reason": "awaiting_human with unchanged source — skipping",
            }

    # Compute scores
    uncertainty = compute_uncertainty(body)
    materiality = compute_materiality(body)

    # If enrichable and high uncertainty → uncertain state
    if new_state == "enrichable" and uncertainty > 0.6:
        if is_valid_transition(new_state, "uncertain"):
            new_state = "uncertain"

    # Update chunk
    now = now_iso()
    conn.execute(
        "UPDATE context_chunks SET state = ?, materiality_score = ?, "
        "last_ai_attempt_at = ?, updated_at = ? WHERE chunk_id = ?",
        (new_state, materiality, now, now, chunk_id),
    )

    enrich_policy = config["default_enrich_policy"]

    log_enrichment_run(
        conn,
        "assess_context",
        "success",
        source_hash,
        chunk_id=chunk_id,
        session_id=session_id,
        started_at=started,
    )

    return {
        "chunk_id": chunk_id,
        "state": new_state,
        "previous_state": current_state,
        "policy": enrich_policy,
        "materiality": round(materiality, 3),
        "uncertainty": round(uncertainty, 3),
        "should_skip": False,
        "skip_reason": None,
        "signals_detected": signals,
        "annotations_created": len(annotations_created),
    }


# ── Core: Queue Clarification ────────────────────────────────────────────


def queue_clarification(
    conn: sqlite3.Connection,
    chunk_ref: str,
    max_questions: int = 5,
) -> dict[str, Any]:
    """Generate AWAITING_HUMAN block with focused questions. Locks chunk.

    Returns dict with: chunk_id, questions, awaiting_human_block.
    """
    config = load_config()
    started = now_iso()

    if not config["enabled"]:
        return {"status": "disabled"}

    max_q = min(max_questions, config["max_questions_per_chunk"])

    row = conn.execute(
        "SELECT * FROM context_chunks WHERE chunk_id = ?", (chunk_ref,)
    ).fetchone()
    if row is None:
        return {"error": f"Chunk '{chunk_ref}' not found"}

    chunk_id = row["chunk_id"]
    current_state = row["state"]
    body = row["body"]
    source_hash = row["source_hash"]

    if current_state == "frozen":
        log_enrichment_run(
            conn,
            "queue_clarification",
            "blocked",
            source_hash,
            chunk_id=chunk_id,
            reason_code="frozen",
            started_at=started,
        )
        return {"error": "Chunk is frozen — cannot queue clarification"}

    # Generate questions based on content analysis
    questions = _generate_questions(body, max_q)

    # Create question records
    question_ids = []
    for q in questions:
        qid = _new_id()
        conn.execute(
            "INSERT INTO context_questions "
            "(question_id, chunk_id, question_text, question_type, priority_score, "
            "state, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (qid, chunk_id, q["text"], q["type"], q["priority"], now_iso()),
        )
        question_ids.append(qid)

    # Build AWAITING_HUMAN block
    now = now_iso()
    block_lines = [
        f"--- AWAITING_HUMAN | {now} ---",
        f"source_hash: {source_hash}",
        "reason: insufficient_clarity",
        "questions:",
    ]
    for i, q in enumerate(questions, 1):
        block_lines.append(f"{i}. {q['text']}")
    block_lines.append("status: blocked_until_human_update")
    block_text = "\n".join(block_lines)

    # Create annotation
    _create_annotation(conn, chunk_id, "ai", "awaiting_human", block_text, source_hash)

    # Transition to awaiting_human
    if is_valid_transition(current_state, "awaiting_human"):
        conn.execute(
            "UPDATE context_chunks SET state = 'awaiting_human', "
            "last_ai_attempt_at = ?, updated_at = ? WHERE chunk_id = ?",
            (now, now, chunk_id),
        )

    log_enrichment_run(
        conn,
        "queue_clarification",
        "success",
        source_hash,
        chunk_id=chunk_id,
        started_at=started,
    )

    return {
        "chunk_id": chunk_id,
        "state": "awaiting_human",
        "questions": [{"id": qid, **q} for qid, q in zip(question_ids, questions)],
        "awaiting_human_block": block_text,
    }


# ── Core: Record Human Answer ────────────────────────────────────────────


def record_human_answer(
    conn: sqlite3.Connection,
    chunk_ref: str,
    answer_text: str,
    question_id: str | None = None,
) -> dict[str, Any]:
    """Ingest human answer, update state, resolve questions.

    Returns dict with: chunk_id, new_state, questions_resolved.
    """
    config = load_config()
    started = now_iso()

    if not config["enabled"]:
        return {"status": "disabled"}

    row = conn.execute(
        "SELECT * FROM context_chunks WHERE chunk_id = ?", (chunk_ref,)
    ).fetchone()
    if row is None:
        return {"error": f"Chunk '{chunk_ref}' not found"}

    chunk_id = row["chunk_id"]
    current_state = row["state"]

    if current_state == "frozen":
        return {"error": "Chunk is frozen — cannot record answer"}

    now = now_iso()
    questions_resolved = 0

    if question_id:
        # Resolve specific question
        conn.execute(
            "UPDATE context_questions SET state = 'answered', answered_by = 'human', "
            "answered_at = ?, answer_text = ? WHERE question_id = ? AND chunk_id = ?",
            (now, answer_text, question_id, chunk_id),
        )
        questions_resolved = 1
    else:
        # Resolve all open questions for this chunk
        result = conn.execute(
            "UPDATE context_questions SET state = 'answered', answered_by = 'human', "
            "answered_at = ?, answer_text = ? WHERE chunk_id = ? AND state = 'open'",
            (now, answer_text, chunk_id),
        )
        questions_resolved = result.rowcount

    # Create resolution annotation
    _create_annotation(
        conn,
        chunk_id,
        "human",
        "resolution",
        f"Human answer: {answer_text}",
        row["source_hash"],
    )

    # Recompute source hash (content is now enriched with human context)
    new_hash = compute_source_hash(row["body"] + "\n" + answer_text)

    # Transition back to enrichable
    new_state = current_state
    if current_state in ("awaiting_human", "uncertain"):
        if is_valid_transition(current_state, "enrichable"):
            new_state = "enrichable"

    conn.execute(
        "UPDATE context_chunks SET state = ?, source_hash = ?, "
        "last_human_update_at = ?, updated_at = ? WHERE chunk_id = ?",
        (new_state, new_hash, now, now, chunk_id),
    )

    log_enrichment_run(
        conn,
        "record_human_answer",
        "success",
        new_hash,
        chunk_id=chunk_id,
        started_at=started,
    )

    return {
        "chunk_id": chunk_id,
        "new_state": new_state,
        "previous_state": current_state,
        "questions_resolved": questions_resolved,
        "source_hash_updated": new_hash != row["source_hash"],
    }


# ── Chunk Ingestion (internal helper for MCP tools) ─────────────────────


def ingest_chunk(
    conn: sqlite3.Connection,
    body: str,
    source_type: str,
    source_ref: str,
    title: str | None = None,
    session_id: str | None = None,
    entity_id: str | None = None,
    language: str = "bg",
) -> dict[str, Any]:
    """Create a new context chunk with default no_enrich state.

    Returns dict with: chunk_id, state, source_hash, signals_detected.
    """
    config = load_config()
    if not config["enabled"]:
        return {"status": "disabled"}

    chunk_id = _new_id()
    source_hash = compute_source_hash(body)
    signals = detect_signals(body) if config["respect_signal_phrases"] else []

    # Determine initial state from signals
    initial_state = "no_enrich"
    if "FREEZE_CONTEXT" in signals:
        initial_state = "frozen"
    elif "ENRICH_OK" in signals:
        initial_state = "enrichable"

    now = now_iso()
    conn.execute(
        "INSERT INTO context_chunks "
        "(chunk_id, session_id, entity_id, source_type, source_ref, source_hash, "
        "title, body, language, state, enrich_policy, materiality_score, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chunk_id,
            session_id,
            entity_id,
            source_type,
            source_ref,
            source_hash,
            title,
            body,
            language,
            initial_state,
            config["default_enrich_policy"],
            0.0,
            now,
            now,
        ),
    )

    return {
        "chunk_id": chunk_id,
        "state": initial_state,
        "source_hash": source_hash,
        "signals_detected": signals,
    }


# ── Internal Helpers ─────────────────────────────────────────────────────


def _create_annotation(
    conn: sqlite3.Connection,
    chunk_id: str,
    author_type: str,
    annotation_type: str,
    body: str,
    source_hash_seen: str | None = None,
) -> str:
    """Insert a context annotation. Returns annotation_id."""
    ann_id = _new_id()
    conn.execute(
        "INSERT INTO context_annotations "
        "(annotation_id, chunk_id, author_type, annotation_type, body, "
        "source_hash_seen, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            ann_id,
            chunk_id,
            author_type,
            annotation_type,
            body,
            source_hash_seen,
            now_iso(),
        ),
    )
    return ann_id


def _generate_questions(text: str, max_questions: int) -> list[dict[str, Any]]:
    """Generate clarification questions based on text content analysis.

    Heuristic: looks for ambiguity signals and generates typed questions.
    """
    questions: list[dict[str, Any]] = []
    text_lower = text.lower()

    # Scope question — always useful if scope is unclear
    scope_keywords = (
        "mapping",
        "bridge",
        "validation",
        "export",
        "memory",
        "task",
        "note",
        "config",
        "workflow",
    )
    has_scope = any(kw in text_lower for kw in scope_keywords)
    if not has_scope and len(questions) < max_questions:
        questions.append(
            {
                "text": "Към кой workflow се отнася: memory, bridge, mapping, export или validation?",
                "type": "scope",
                "priority": 0.9,
            }
        )

    # Temporal question — if no dates/times mentioned
    time_pattern = re.compile(
        r"\b(\d{4}-\d{2}|\d{2}\.\d{2}\.\d{4}|днес|утре|вчера|"
        r"today|tomorrow|yesterday)\b",
        re.IGNORECASE,
    )
    if not time_pattern.search(text) and len(questions) < max_questions:
        questions.append(
            {
                "text": "Това описва текущо правило, историческа бележка или идея за бъдеща промяна?",
                "type": "time",
                "priority": 0.7,
            }
        )

    # Semantics question — if text is very short or ambiguous
    if len(text.split()) < 30 and len(questions) < max_questions:
        questions.append(
            {
                "text": "Може ли да разшириш контекста — какъв е конкретният проблем или решение?",
                "type": "semantics",
                "priority": 0.8,
            }
        )

    # Action question — if no clear actionable items
    action_keywords = (
        "трябва",
        "направи",
        "fix",
        "add",
        "remove",
        "update",
        "create",
        "should",
        "must",
        "need",
    )
    has_action = any(kw in text_lower for kw in action_keywords)
    if not has_action and len(questions) < max_questions:
        questions.append(
            {
                "text": "Има ли конкретно действие, което трябва да се предприеме?",
                "type": "action",
                "priority": 0.6,
            }
        )

    # Downstream use question
    if len(questions) < max_questions:
        questions.append(
            {
                "text": "Това трябва ли да влезе в canonical knowledge или да остане като note?",
                "type": "downstream_use",
                "priority": 0.5,
            }
        )

    # Sort by priority descending, limit
    questions.sort(key=lambda q: q["priority"], reverse=True)
    return questions[:max_questions]
