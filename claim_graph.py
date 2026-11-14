"""Claim Graph — Candidate knowledge extraction, evidence, and promotion.

Phase 2 of Intelligence v2:
- Typed (Subject, Predicate, Object, Scope) claim extraction from text
- Evidence attachment with provenance tracking
- Governance gates for promotion to canonical facts
- Separate operator review before any new canonical promotion
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


def _resolve_review_scope(body: str, claimed_scope: str | None, human_required_scopes: list[str]):
    """Preserve every detected sensitive flag despite a caller's scope hint."""
    lower = body.lower()
    sensitive = sorted(scope for scope in human_required_scopes
                       if any(word in lower for word in _SCOPE_KEYWORDS.get(scope, ())))
    detected = _detect_scope(body)
    effective = claimed_scope or detected
    if sensitive and effective not in sensitive:
        effective = detected if detected in sensitive else sensitive[0]
    return effective, sensitive


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
    scope, _ = _resolve_review_scope(body, scope_hint, config.get("human_required_scopes", []))

    # All canonical promotions require separate operator review, whatever the
    # heuristic confidence or source scope. The flag now tells the same truth.
    requires_human = True

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


def promote_candidate(
    conn: sqlite3.Connection,
    claim_id: str,
    mode: str = "human_confirmed",
    approval_id: str | None = None,
    claim_kind: str = "candidate",
) -> dict[str, Any]:
    """Promote only a fresh snapshot approved through the separate operator CLI.

    A mode string or agent-reported human answer is not approval. Automatic
    confidence/evidence modes retain candidates for review, never mint facts.
    Trusted bridge replication is a separate operation, not this promotion API.
    """
    if mode != "human_confirmed":
        return {"claim_id": claim_id, "promoted": False,
                "reason": "Automatic/imported promotion modes require operator review",
                "review_required": True}
    from promotion_governance import consume_approval
    return consume_approval(conn, claim_kind, claim_id, approval_id)


def _write_approved_candidate(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    *,
    approval_id: str,
    content_sha256: str,
) -> dict[str, Any]:
    """Internal writer: the caller holds the checked approval/savepoint.

    Not an MCP entrypoint or a boundary against arbitrary local Python.
    Required provenance, events and graph writes must propagate failures.
    """
    claim_id = row["claim_id"]
    claim_scope = row["claim_scope"]
    mode = "human_confirmed"
    started = now_iso()
    approval_provenance = {"approval_id": approval_id,
                           "content_sha256": content_sha256,
                           "approved_via": "operator_cli"}
    # Promote: create canonical fact
    now = now_iso()
    evidence_rows = conn.execute(
        "SELECT evidence_type, evidence_ref, excerpt, source_start, source_end "
        "FROM claim_evidence WHERE claim_id = ?", (claim_id,),
    ).fetchall()

    existing_same = conn.execute(
        "SELECT fact_id FROM canonical_facts "
        "WHERE subject = ? AND predicate = ? AND object_text = ? "
        "AND fact_scope = ? AND object_type = ? "
        "AND COALESCE(valid_to, '') = '' "
        "AND COALESCE(superseded_by_fact_id, '') = '' "
        "ORDER BY updated_at DESC LIMIT 1",
        (row["subject"], row["predicate"], row["object_text"], row["claim_scope"], row["object_type"]),
    ).fetchone()
    if existing_same:
        fact_id = existing_same["fact_id"]
        conn.execute(
            "UPDATE candidate_claims SET status = 'promoted', promoted_to_fact_id = ?, "
            "updated_at = ? WHERE claim_id = ?",
            (fact_id, now, claim_id),
        )
        _add_promotion_lineage(conn, row, fact_id, evidence_rows, now, approval_id)
        record_memory_event(
            conn,
            event_type="fact_reinforce",
            aggregate_kind="fact",
            aggregate_id=fact_id,
            tool_name="sqlite-intel.promote_candidate",
            event_ts=now,
            new_value={"claim_id": claim_id, "mode": mode, **approval_provenance},
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

    # Build provenance summary from the reviewed source set.
    provenance = f"Promoted via operator CLI approval {approval_id} from claim {claim_id}. "
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
    _add_promotion_lineage(conn, row, fact_id, evidence_rows, now, approval_id)

    competing_rows = conn.execute(
        "SELECT fact_id FROM canonical_facts "
        "WHERE fact_id != ? AND subject = ? AND predicate = ? "
        "AND object_text != ? AND COALESCE(valid_to, '') = '' "
        "AND COALESCE(superseded_by_fact_id, '') = ''",
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
            **approval_provenance,
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


def _add_promotion_lineage(conn, row, fact_id, evidence_rows, now, approval_id):
    """Required lineage is identical for new facts and existing-fact reuse."""
    claim_id = row["claim_id"]
    add_provenance_link(
        conn, subject_kind="fact", subject_ref=fact_id,
        source_kind="claim", source_ref=claim_id,
        excerpt=f"Operator-approved claim {claim_id}",
        confidence=row["confidence"], created_at=now,
    )
    for evidence in evidence_rows:
        add_provenance_link(
            conn, subject_kind="fact", subject_ref=fact_id,
            source_kind=evidence["evidence_type"], source_ref=evidence["evidence_ref"],
            span_start=evidence["source_start"], span_end=evidence["source_end"],
            excerpt=evidence["excerpt"], confidence=row["confidence"], created_at=now,
        )
    from impact_graph import add_impact_edge
    add_impact_edge(
        conn, source_kind="chunk", source_ref=row["chunk_id"],
        target_kind="fact", target_ref=fact_id, impact_type="informs",
        impact_score=row["confidence"],
        rationale=f"Claim {claim_id} promoted via operator approval {approval_id}",
    )


def auto_promote_layer1(
    conn: sqlite3.Connection,
    claims: list[dict[str, Any]],
    threshold: float = 0.7,
) -> list[dict[str, Any]]:
    """Compatibility hook: extracted claims stay candidates for operator review."""
    return []
