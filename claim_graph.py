"""Claim Graph — Candidate knowledge extraction, evidence, and promotion.

Phase 2 of Intelligence v2:
- Typed (Subject, Predicate, Object, Scope) claim extraction from text
- Evidence attachment with provenance tracking
- Governance gates for promotion to canonical facts
- Human-required vs auto-promotable scope policies
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from db_utils import (
    add_knowledge_link,
    add_provenance_link,
    now_iso,
    record_memory_event,
)
from intelligence_v2 import (
    _new_id,
    load_config,
    log_enrichment_run,
)
from enrichment_constants import _PREDICATE_BASE_CONFIDENCE

_log = logging.getLogger("claim_graph")

# ── Claim Extraction Heuristics ──────────────────────────────────────────

# Patterns for typed claim extraction (simple heuristic, no NLP dependency)
# Format: subject → predicate → object

# "X uses Y", "X depends on Y", "X is Y"
_RELATION_PATTERNS = [
    # English patterns
    (
        re.compile(
            r"(\b\w[\w\s]{1,40}?)\s+(?:uses?|използва)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
            re.I,
        ),
        "uses",
    ),
    (
        re.compile(
            r"(\b\w[\w\s]{1,40}?)\s+(?:depends?\s+on|зависи\s+от)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
            re.I,
        ),
        "depends_on",
    ),
    (
        re.compile(
            r"(\b\w[\w\s]{1,40}?)\s+(?:is|е|са)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)", re.I
        ),
        "is",
    ),
    (
        re.compile(
            r"(\b\w[\w\s]{1,40}?)\s+(?:requires?|изисква)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
            re.I,
        ),
        "requires",
    ),
    (
        re.compile(
            r"(\b\w[\w\s]{1,40}?)\s+(?:produces?|генерира|създава)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
            re.I,
        ),
        "produces",
    ),
    (
        re.compile(
            r"(\b\w[\w\s]{1,40}?)\s+(?:validates?|валидира)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
            re.I,
        ),
        "validates",
    ),
    (
        re.compile(
            r"(\b\w[\w\s]{1,40}?)\s+(?:contains?|съдържа)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
            re.I,
        ),
        "contains",
    ),
    (
        re.compile(
            r"(\b\w[\w\s]{1,40}?)\s+(?:replaces?|замества|заменя)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
            re.I,
        ),
        "replaces",
    ),
]

# Scope detection keywords
_SCOPE_KEYWORDS = {
    "memory": ("memory", "entity", "observation", "relation", "knowledge", "памет"),
    "bridge": ("bridge", "sync", "push", "pull", "мост", "синхронизация"),
    "mapping": ("mapping", "map", "column", "template", "шаблон", "маппинг"),
    "validation": ("validation", "validate", "check", "audit", "валидация"),
    "export": ("export", "excel", "pdf", "report", "експорт", "отчет"),
}


def _detect_scope(text: str) -> str:
    """Detect the most likely scope from text content."""
    text_lower = text.lower()
    scores: dict[str, int] = {}
    for scope, keywords in _SCOPE_KEYWORDS.items():
        scores[scope] = sum(1 for kw in keywords if kw in text_lower)

    if not any(scores.values()):
        return "memory"  # default scope

    return max(scores, key=scores.get)


def _extract_spo_tuples(text: str) -> list[dict[str, Any]]:
    """Extract (Subject, Predicate, Object) tuples from text using patterns."""
    tuples: list[dict[str, Any]] = []
    seen = set()

    for pattern, predicate in _RELATION_PATTERNS:
        for match in pattern.finditer(text):
            subject = match.group(1).strip()
            obj = match.group(2).strip()

            # Skip trivial matches
            if len(subject) < 3 or len(obj) < 3:
                continue
            if subject.lower() == obj.lower():
                continue

            key = (subject.lower(), predicate, obj.lower())
            if key not in seen:
                seen.add(key)
                tuples.append(
                    {
                        "subject": subject,
                        "predicate": predicate,
                        "object": obj,
                        "start": match.start(0),
                        "end": match.end(0),
                        "excerpt": match.group(0).strip(),
                    }
                )

    return tuples


# ── Core: Extract Candidate Claims ───────────────────────────────────────


def extract_candidate_claims(
    conn: sqlite3.Connection,
    chunk_ref: str,
    scope_hint: str | None = None,
) -> dict[str, Any]:
    """Extract typed (S, P, O, scope) claims with evidence from a context chunk.

    Returns dict with: chunk_id, claims_extracted, claims.
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
    state = row["state"]
    body = row["body"]

    # Only extract from enrichable or uncertain chunks
    if state not in ("enrichable", "uncertain"):
        log_enrichment_run(
            conn,
            "extract_candidate_claims",
            "blocked",
            chunk_id,
            chunk_id=chunk_id,
            reason_code=f"invalid_state:{state}",
            started_at=started,
        )
        return {
            "error": f"Chunk state '{state}' does not allow claim extraction. "
            "State must be 'enrichable' or 'uncertain'.",
        }

    # Extract SPO tuples
    tuples = _extract_spo_tuples(body)
    scope = scope_hint or _detect_scope(body)

    # Determine if human confirmation required
    requires_human = scope in config.get("human_required_scopes", [])

    claims_created: list[dict[str, Any]] = []
    now = now_iso()

    for t in tuples:
        claim_id = _new_id()

        # Adaptive confidence from predicate type (Layer 2 parity)
        confidence = _PREDICATE_BASE_CONFIDENCE.get(t["predicate"], 0.5)

        conn.execute(
            "INSERT INTO candidate_claims "
            "(claim_id, chunk_id, subject, predicate, object_text, object_type, "
            "claim_scope, confidence, status, requires_human, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'text', ?, ?, 'candidate', ?, ?, ?)",
            (
                claim_id,
                chunk_id,
                t["subject"],
                t["predicate"],
                t["object"],
                scope,
                confidence,
                1 if requires_human else 0,
                now,
                now,
            ),
        )

        # Create evidence record linking claim to source chunk
        evidence_id = _new_id()
        conn.execute(
            "INSERT INTO claim_evidence "
            "(evidence_id, claim_id, evidence_type, evidence_ref, weight, excerpt, "
            "source_start, source_end, created_at) "
            "VALUES (?, ?, 'source_chunk', ?, 1.0, ?, ?, ?, ?)",
            (
                evidence_id,
                claim_id,
                chunk_id,
                t.get("excerpt") or (body[:200] if len(body) > 200 else body),
                t.get("start"),
                t.get("end"),
                now,
            ),
        )
        add_provenance_link(
            conn,
            subject_kind="claim",
            subject_ref=claim_id,
            source_kind="chunk",
            source_ref=chunk_id,
            span_start=t.get("start"),
            span_end=t.get("end"),
            excerpt=t.get("excerpt"),
            confidence=confidence,
            created_at=now,
        )
        record_memory_event(
            conn,
            event_type="claim_extract",
            aggregate_kind="claim",
            aggregate_id=claim_id,
            tool_name="sqlite-intel.extract_candidate_claims",
            event_ts=now,
            new_value={
                "subject": t["subject"],
                "predicate": t["predicate"],
                "object": t["object"],
                "scope": scope,
                "confidence": confidence,
            },
            source_kind="chunk",
            source_ref=chunk_id,
            source_excerpt=t.get("excerpt"),
            source_start=t.get("start"),
            source_end=t.get("end"),
        )

        claims_created.append(
            {
                "claim_id": claim_id,
                "subject": t["subject"],
                "predicate": t["predicate"],
                "object": t["object"],
                "scope": scope,
                "confidence": confidence,
                "requires_human": requires_human,
            }
        )

    log_enrichment_run(
        conn,
        "extract_candidate_claims",
        "success",
        chunk_id,
        chunk_id=chunk_id,
        started_at=started,
    )

    return {
        "chunk_id": chunk_id,
        "claims_extracted": len(claims_created),
        "scope": scope,
        "claims": claims_created,
    }


# ── Core: Promote Candidate ──────────────────────────────────────────────

# Promotion modes
PROMOTION_MODES = ("human_confirmed", "multi_evidence", "imported", "auto_layer1")

# Minimum evidence count for auto-promotion
_MIN_EVIDENCE_FOR_AUTO = 3
_MIN_CONFIDENCE_FOR_AUTO = 0.7


def promote_candidate(
    conn: sqlite3.Connection,
    claim_id: str,
    mode: str = "human_confirmed",
) -> dict[str, Any]:
    """Governance gate: candidate → canonical_fact.

    Modes:
    - human_confirmed: explicit human approval (always allowed)
    - multi_evidence: multiple independent evidence points (policy-gated)
    - imported: bulk import from trusted source

    Returns dict with: claim_id, promoted, fact_id, reason.
    """
    config = load_config()
    started = now_iso()

    if not config["enabled"]:
        return {"status": "disabled"}

    if mode not in PROMOTION_MODES:
        return {
            "error": f"Invalid promotion mode: {mode}. Use one of: {PROMOTION_MODES}"
        }

    row = conn.execute(
        "SELECT * FROM candidate_claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    if row is None:
        return {"error": f"Claim '{claim_id}' not found"}

    claim_scope = row["claim_scope"]
    status = row["status"]
    requires_human = bool(row["requires_human"])

    if status != "candidate":
        return {"error": f"Claim status is '{status}', expected 'candidate'"}

    # Governance gate: sensitive scopes require human confirmation
    human_required_scopes = config.get("human_required_scopes", [])
    auto_promote_scopes = config.get("auto_promote_scopes", [])

    if claim_scope in human_required_scopes and mode != "human_confirmed":
        log_enrichment_run(
            conn,
            "promote_candidate",
            "blocked",
            claim_id,
            reason_code=f"human_required_for_{claim_scope}",
            started_at=started,
        )
        return {
            "claim_id": claim_id,
            "promoted": False,
            "reason": f"Scope '{claim_scope}' requires human confirmation. "
            f"Use mode='human_confirmed' to promote.",
        }

    # Auto Layer 1: confidence-only check, no evidence count requirement
    if mode == "auto_layer1":
        if claim_scope not in auto_promote_scopes:
            log_enrichment_run(
                conn,
                "promote_candidate",
                "blocked",
                claim_id,
                reason_code=f"scope_{claim_scope}_not_auto",
                started_at=started,
            )
            return {
                "claim_id": claim_id,
                "promoted": False,
                "reason": f"Scope '{claim_scope}' not in auto_promote_scopes.",
            }
        if row["confidence"] < _MIN_CONFIDENCE_FOR_AUTO:
            return {
                "claim_id": claim_id,
                "promoted": False,
                "reason": f"Confidence {row['confidence']:.2f} < {_MIN_CONFIDENCE_FOR_AUTO}",
            }
        if requires_human:
            return {
                "claim_id": claim_id,
                "promoted": False,
                "reason": "Claim requires human confirmation",
            }

    # Multi-evidence validation
    if mode == "multi_evidence":
        if claim_scope not in auto_promote_scopes:
            return {
                "claim_id": claim_id,
                "promoted": False,
                "reason": f"Scope '{claim_scope}' not in auto_promote_scopes. "
                f"Only {auto_promote_scopes} allow multi_evidence promotion.",
            }

        evidence_count = conn.execute(
            "SELECT COUNT(*) FROM claim_evidence WHERE claim_id = ?", (claim_id,)
        ).fetchone()[0]

        if evidence_count < _MIN_EVIDENCE_FOR_AUTO:
            return {
                "claim_id": claim_id,
                "promoted": False,
                "reason": f"Insufficient evidence ({evidence_count}/{_MIN_EVIDENCE_FOR_AUTO}) "
                f"for auto-promotion.",
            }

        if row["confidence"] < _MIN_CONFIDENCE_FOR_AUTO:
            return {
                "claim_id": claim_id,
                "promoted": False,
                "reason": f"Confidence {row['confidence']:.2f} below threshold "
                f"{_MIN_CONFIDENCE_FOR_AUTO}.",
            }

    # Promote: create canonical fact
    now = now_iso()

    existing_same = conn.execute(
        "SELECT fact_id FROM canonical_facts "
        "WHERE subject = ? AND predicate = ? AND object_text = ? "
        "AND COALESCE(valid_to, '') = '' "
        "ORDER BY updated_at DESC LIMIT 1",
        (row["subject"], row["predicate"], row["object_text"]),
    ).fetchone()
    if existing_same:
        fact_id = existing_same["fact_id"]
        conn.execute(
            "UPDATE candidate_claims SET status = 'promoted', promoted_to_fact_id = ?, "
            "updated_at = ? WHERE claim_id = ?",
            (fact_id, now, claim_id),
        )
        add_provenance_link(
            conn,
            subject_kind="fact",
            subject_ref=fact_id,
            source_kind="claim",
            source_ref=claim_id,
            excerpt=f"Existing fact reinforced by claim {claim_id}",
            created_at=now,
        )
        record_memory_event(
            conn,
            event_type="fact_reinforce",
            aggregate_kind="fact",
            aggregate_id=fact_id,
            tool_name="sqlite-intel.promote_candidate",
            event_ts=now,
            new_value={"claim_id": claim_id, "mode": mode},
            source_kind="claim",
            source_ref=claim_id,
        )
        return {
            "claim_id": claim_id,
            "promoted": True,
            "fact_id": fact_id,
            "subject": row["subject"],
            "predicate": row["predicate"],
            "object": row["object_text"],
            "scope": claim_scope,
            "validation_mode": mode,
            "reused_existing_fact": True,
        }

    fact_id = _new_id()

    # Build provenance summary
    evidence_rows = conn.execute(
        "SELECT evidence_type, evidence_ref, excerpt, source_start, source_end "
        "FROM claim_evidence WHERE claim_id = ?",
        (claim_id,),
    ).fetchall()
    provenance = f"Promoted via {mode} from claim {claim_id}. "
    provenance += f"Evidence: {len(evidence_rows)} sources "
    provenance += (
        "("
        + ", ".join(
            f"{r['evidence_type']}:{r['evidence_ref'][:16]}" for r in evidence_rows[:5]
        )
        + ")"
    )

    conn.execute(
        "INSERT INTO canonical_facts "
        "(fact_id, subject, predicate, object_text, object_type, fact_scope, "
        "provenance_summary, confidence, validation_mode, source_claim_id, "
        "valid_from, valid_to, superseded_by_fact_id, contradiction_count, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)",
        (
            fact_id,
            row["subject"],
            row["predicate"],
            row["object_text"],
            row["object_type"],
            claim_scope,
            provenance,
            row["confidence"],
            mode,
            claim_id,
            now,
            now,
            now,
        ),
    )

    # Update claim status
    conn.execute(
        "UPDATE candidate_claims SET status = 'promoted', promoted_to_fact_id = ?, "
        "updated_at = ? WHERE claim_id = ?",
        (fact_id, now, claim_id),
    )
    add_provenance_link(
        conn,
        subject_kind="fact",
        subject_ref=fact_id,
        source_kind="claim",
        source_ref=claim_id,
        excerpt=f"Promoted from claim {claim_id}",
        confidence=row["confidence"],
        created_at=now,
    )
    for ev in evidence_rows:
        add_provenance_link(
            conn,
            subject_kind="fact",
            subject_ref=fact_id,
            source_kind=ev["evidence_type"],
            source_ref=ev["evidence_ref"],
            span_start=ev["source_start"],
            span_end=ev["source_end"],
            excerpt=ev["excerpt"],
            confidence=row["confidence"],
            created_at=now,
        )

    # Create impact edge: source_chunk → promoted fact
    try:
        from impact_graph import add_impact_edge

        add_impact_edge(
            conn,
            source_kind="chunk",
            source_ref=row["chunk_id"],
            target_kind="fact",
            target_ref=fact_id,
            impact_type="informs",
            impact_score=row["confidence"],
            rationale=f"Claim {claim_id} promoted via {mode}",
        )
    except Exception as e:
        _log.debug("impact_graph link failed: %s", e)

    competing_rows = conn.execute(
        "SELECT fact_id FROM canonical_facts "
        "WHERE fact_id != ? AND subject = ? AND predicate = ? "
        "AND object_text != ? AND COALESCE(valid_to, '') = ''",
        (fact_id, row["subject"], row["predicate"], row["object_text"]),
    ).fetchall()
    contradiction_fact_ids = {fact_id}
    for comp in competing_rows:
        contradiction_fact_ids.add(comp["fact_id"])
        add_knowledge_link(
            conn,
            subject_kind="fact",
            subject_ref=fact_id,
            relation_type="contradicts",
            object_kind="fact",
            object_ref=comp["fact_id"],
            rationale=f"Same subject/predicate, different object after claim {claim_id}",
            created_at=now,
        )
        add_knowledge_link(
            conn,
            subject_kind="fact",
            subject_ref=comp["fact_id"],
            relation_type="contradicts",
            object_kind="fact",
            object_ref=fact_id,
            rationale=f"Same subject/predicate, different object after claim {claim_id}",
            created_at=now,
        )
    if competing_rows:
        # knowledge_links is authoritative; derive the cached counts instead
        # of maintaining a second, drift-prone +1 implementation here.
        from memory_audit import _refresh_contradiction_counts

        _refresh_contradiction_counts(conn, contradiction_fact_ids)

    record_memory_event(
        conn,
        event_type="fact_promote",
        aggregate_kind="fact",
        aggregate_id=fact_id,
        tool_name="sqlite-intel.promote_candidate",
        event_ts=now,
        new_value={
            "subject": row["subject"],
            "predicate": row["predicate"],
            "object": row["object_text"],
            "claim_id": claim_id,
            "mode": mode,
        },
        source_kind="claim",
        source_ref=claim_id,
    )

    log_enrichment_run(
        conn, "promote_candidate", "success", claim_id, started_at=started
    )

    return {
        "claim_id": claim_id,
        "promoted": True,
        "fact_id": fact_id,
        "subject": row["subject"],
        "predicate": row["predicate"],
        "object": row["object_text"],
        "scope": claim_scope,
        "validation_mode": mode,
    }


def auto_promote_layer1(
    conn: sqlite3.Connection,
    claims: list[dict[str, Any]],
    threshold: float = 0.7,
) -> list[dict[str, Any]]:
    """Auto-promote high-confidence Layer 1 claims. Returns list of promoted results."""
    promoted = []
    for c in claims:
        if c.get("requires_human"):
            continue
        if c.get("confidence", 0) < threshold:
            continue
        # Dedup check against existing canonical facts
        exists = conn.execute(
            "SELECT 1 FROM canonical_facts WHERE subject = ? AND predicate = ? AND object_text = ?",
            (c["subject"], c["predicate"], c["object"]),
        ).fetchone()
        if exists:
            continue
        result = promote_candidate(conn, c["claim_id"], mode="auto_layer1")
        if result.get("promoted"):
            promoted.append(result)
    return promoted
