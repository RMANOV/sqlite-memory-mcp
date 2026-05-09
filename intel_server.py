#!/usr/bin/env python3
"""Thin MCP server exposing Intelligence v2 tools.

Shares the same SQLite database as the main sqlite-kb server.
Exists because Claude Code 2.x has a tool-count limit per MCP server
(~9 tools visible out of 50), so intelligence tools are split into a separate server.
"""

from __future__ import annotations

import json

from fastmcp_compat import FastMCP

from db_utils import (
    get_conn as _get_conn,
    setup_logger,
)
from intelligence_v2 import (
    assess_context as _assess_context,
    queue_clarification as _queue_clarification,
    record_human_answer as _record_human_answer,
    load_config as _load_intel_config,
)
from claim_graph import (
    extract_candidate_claims as _extract_claims,
    promote_candidate as _promote_candidate,
    auto_promote_layer1 as _auto_promote_layer1,
)
from context_packer import (
    build_context_pack as _build_pack,
    warm_recent_task_packs as _warm_task_packs,
)
from impact_graph import explain_impact as _explain_impact
from memory_audit import (
    govern_fact as _govern_fact,
    list_memory_audit_issues as _list_memory_audit_issues,
    replay_memory_events as _replay_memory_events,
    run_memory_audit as _run_memory_audit,
)
from reflection import (
    audit_reflection_candidates as _audit_reflection_candidates,
    format_audit_markdown as _format_audit_markdown,
)
from premium_runtime import maybe_mount_premium_extensions

# ── Logging (file-only, NEVER stdout — breaks MCP stdio) ────────────────

logger = setup_logger("sqlite-intel", "intel_server.log")

# ── FastMCP app ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "sqlite-intel",
    instructions=(
        "Intelligence v2 tools: context assessment, claim extraction, knowledge tiers. "
        "Shares DB with sqlite-kb."
    ),
)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 1: assess_context
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def assess_context(
    chunk_ref: str,
    session_id: str | None = None,
    force: bool = False,
) -> str:
    """Classify context chunk, detect signals, determine state transition.

    Scans for signal phrases (ENRICH_OK, NO_ENRICH, WAIT_HUMAN, FREEZE_CONTEXT),
    computes materiality and uncertainty scores, and manages state transitions.
    Skips reprocessing if chunk is awaiting_human with unchanged source_hash.

    Args:
        chunk_ref: ID of the context chunk to assess
        session_id: Optional session context
        force: If True, bypass skip logic and frozen state
    """
    try:
        with _get_conn() as conn:
            result = _assess_context(conn, chunk_ref, session_id, force)
            logger.info(
                "assess_context: chunk=%s, state=%s", chunk_ref, result.get("state")
            )
            return json.dumps(result)
    except Exception as exc:
        logger.error("assess_context failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 2: queue_clarification
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def queue_clarification(
    chunk_ref: str,
    max_questions: int = 5,
) -> str:
    """Generate AWAITING_HUMAN block with focused clarification questions.

    Analyzes the chunk content to produce typed questions (scope, semantics,
    time, action, downstream_use) and locks the chunk until human answers.

    Args:
        chunk_ref: ID of the context chunk
        max_questions: Maximum number of questions to generate (1-5)
    """
    try:
        with _get_conn() as conn:
            result = _queue_clarification(conn, chunk_ref, max_questions)
            logger.info(
                "queue_clarification: chunk=%s, questions=%d",
                chunk_ref,
                len(result.get("questions", [])),
            )
            return json.dumps(result)
    except Exception as exc:
        logger.error("queue_clarification failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 3: record_human_answer
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def record_human_answer(
    chunk_ref: str,
    answer_text: str,
    question_id: str | None = None,
) -> str:
    """Ingest human answer, update chunk state, resolve open questions.

    Transitions chunk from awaiting_human/uncertain back to enrichable,
    updates source_hash to reflect the new information.

    Args:
        chunk_ref: ID of the context chunk
        answer_text: Human's answer text
        question_id: Optional specific question to answer (answers all if omitted)
    """
    try:
        with _get_conn() as conn:
            result = _record_human_answer(conn, chunk_ref, answer_text, question_id)
            logger.info(
                "record_human_answer: chunk=%s, state=%s",
                chunk_ref,
                result.get("state"),
            )
            return json.dumps(result)
    except Exception as exc:
        logger.error("record_human_answer failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 4: extract_candidate_claims
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def extract_candidate_claims(
    chunk_ref: str,
    scope_hint: str | None = None,
) -> str:
    """Extract typed (subject, predicate, object, scope) claims from a context chunk.

    Only works on enrichable or uncertain chunks. Creates candidate claims with
    evidence records linking back to the source. Claims require governance gate
    (promote_candidate) before becoming canonical facts.

    Args:
        chunk_ref: ID of the context chunk to extract from
        scope_hint: Optional scope override (memory|bridge|mapping|validation|export)
    """
    try:
        with _get_conn() as conn:
            result = _extract_claims(conn, chunk_ref, scope_hint)
            logger.info(
                "extract_candidate_claims: chunk=%s, claims=%d",
                chunk_ref,
                result.get("claims_extracted", 0),
            )
            return json.dumps(result)
    except Exception as exc:
        logger.error("extract_candidate_claims failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 5: promote_candidate
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def promote_candidate(
    claim_id: str,
    mode: str = "human_confirmed",
) -> str:
    """Governance gate: promote candidate claim to canonical fact.

    Modes:
    - human_confirmed: explicit human approval (always allowed)
    - multi_evidence: auto-promotion if enough independent evidence (policy-gated)
    - imported: bulk import from trusted source

    Sensitive scopes (mapping, validation, bridge, export) require human_confirmed.

    Args:
        claim_id: ID of the candidate claim
        mode: Promotion mode (human_confirmed|multi_evidence|imported)
    """
    try:
        with _get_conn() as conn:
            result = _promote_candidate(conn, claim_id, mode)
            logger.info(
                "promote_candidate: claim=%s, mode=%s, status=%s",
                claim_id,
                mode,
                result.get("status"),
            )
            return json.dumps(result)
    except Exception as exc:
        logger.error("promote_candidate failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 6: build_context_pack
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def build_context_pack(
    pack_type: str = "executor",
    target_ref: str | None = None,
    session_id: str | None = None,
    token_budget: int | None = None,
) -> str:
    """Compile role-specific context pack with token budget optimization.

    Greedy coverage algorithm: scores available facts, claims, questions, and
    chunks by relevance × role weight, then fills the token budget.

    Pack types:
    - planner: facts + questions (what do we know, what's uncertain)
    - reviewer: facts + claims (what to validate)
    - executor: facts + chunks (confirmed context for implementation)
    - bridge_checker: claims + questions (what needs bridge verification)
    - handoff: everything prioritized for session continuity

    Args:
        pack_type: Role-specific pack type
        target_ref: Optional target reference for context filtering
        session_id: Optional session context
        token_budget: Token limit (default from config, typically 4000)
    """
    try:
        with _get_conn() as conn:
            result = _build_pack(conn, pack_type, target_ref, session_id, token_budget)
            logger.info(
                "build_context_pack: type=%s, tokens=%d",
                pack_type,
                result.get("token_usage", 0),
            )
            return json.dumps(result)
    except Exception as exc:
        logger.error("build_context_pack failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 7: explain_impact
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def explain_impact(
    source_kind: str = "chunk",
    source_ref: str = "",
    depth: str = "standard",
) -> str:
    """Show downstream impact of a knowledge change via bounded BFS.

    Traverses impact_edges graph to find affected sessions, snapshots,
    mappings, validations, and exports. Results grouped and ranked by
    propagated impact score.

    Args:
        source_kind: Type of source (chunk|claim|fact)
        source_ref: ID of the source entity
        depth: Traversal depth (quick=1, standard=3, deep=5)
    """
    try:
        with _get_conn() as conn:
            result = _explain_impact(conn, source_kind, source_ref, depth)
            logger.info(
                "explain_impact: %s=%s, depth=%s, impacts=%d",
                source_kind,
                source_ref,
                depth,
                result.get("total_impacts", 0),
            )
            return json.dumps(result)
    except Exception as exc:
        logger.error("explain_impact failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 8: audit_memory
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def audit_memory(
    repair: bool = True,
    stale_sync_minutes: int = 120,
) -> str:
    """Run the persistent self-repair audit loop over facts, packs, provenance, and sync drift."""
    try:
        with _get_conn() as conn:
            result = _run_memory_audit(
                conn,
                repair=repair,
                stale_sync_minutes=stale_sync_minutes,
            )
            logger.info(
                "audit_memory: open=%d resolved=%d repair=%s",
                result.get("open_issue_count", 0),
                result.get("resolved_issue_count", 0),
                repair,
            )
            return json.dumps(result)
    except Exception as exc:
        logger.error("audit_memory failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 9: replay_memory
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def replay_memory(
    aggregate_kind: str = "",
    aggregate_id: str = "",
    limit: int = 100,
    since_ts: str = "",
) -> str:
    """Replay append-only memory events for a task/fact/chunk or for the whole ledger."""
    try:
        with _get_conn() as conn:
            result = _replay_memory_events(
                conn,
                aggregate_kind=aggregate_kind or None,
                aggregate_id=aggregate_id or None,
                limit=limit,
                since_ts=since_ts or None,
            )
            logger.info(
                "replay_memory: aggregate=%s:%s count=%d",
                aggregate_kind or "*",
                aggregate_id or "*",
                result.get("count", 0),
            )
            return json.dumps(result)
    except Exception as exc:
        logger.error("replay_memory failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 10: govern_fact
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def govern_fact(
    fact_id: str,
    action: str,
    target_fact_id: str = "",
    rationale: str = "",
    effective_at: str = "",
) -> str:
    """Apply truth-maintenance to a fact: supersede, contradict, invalidate, or revalidate."""
    try:
        with _get_conn() as conn:
            result = _govern_fact(
                conn,
                fact_id,
                action,
                target_fact_id=target_fact_id or None,
                rationale=rationale or None,
                effective_at=effective_at or None,
            )
            logger.info(
                "govern_fact: fact=%s action=%s changed=%s",
                fact_id,
                action,
                result.get("changed"),
            )
            return json.dumps(result)
    except Exception as exc:
        logger.error("govern_fact failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 11: list_memory_issues
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def list_memory_issues(status: str = "open", limit: int = 100) -> str:
    """List persisted memory audit issues after the latest audit run."""
    try:
        with _get_conn() as conn:
            result = _list_memory_audit_issues(conn, status=status, limit=limit)
            logger.info(
                "list_memory_issues: status=%s count=%d",
                status,
                result.get("count", 0),
            )
            return json.dumps(result)
    except Exception as exc:
        logger.error("list_memory_issues failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 12: enrich_context
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def enrich_context(depth: str = "quick") -> str:
    """Compatibility wrapper: enriches context at different depth levels.

    Depth levels:
    - quick: assess all enrichable chunks + build executor pack
    - standard: + extract candidate claims
    - deep: + explain impact for all recent facts

    Args:
        depth: Enrichment depth (quick|standard|deep)
    """
    try:
        config = _load_intel_config()
        if not config["enabled"]:
            return json.dumps(
                {"status": "disabled", "message": "Intelligence v2 is disabled"}
            )

        results: dict = {"depth": depth, "steps": []}

        with _get_conn() as conn:
            # Step 1: Assess all enrichable chunks
            enrichable = conn.execute(
                "SELECT chunk_id FROM context_chunks WHERE state = 'enrichable' LIMIT 20"
            ).fetchall()
            assessed = []
            for row in enrichable:
                r = _assess_context(conn, row["chunk_id"])
                assessed.append(r.get("chunk_id", "?"))
            results["steps"].append({"assess": len(assessed)})

            # Step 2: Build executor pack
            pack = _build_pack(conn, "executor")
            results["steps"].append(
                {
                    "pack": pack.get("pack_id"),
                    "tokens": pack.get("token_usage", 0),
                    "relevance": pack.get("relevance_score", 0.0),
                    "quality": pack.get("quality_score", 0.0),
                }
            )
            results["pack_body"] = pack.get("body", "")
            task_pack_stats = _warm_task_packs(conn, pack_type="executor", limit=8)
            results["steps"].append(task_pack_stats)

            if depth in ("standard", "deep"):
                # Step 3: Extract claims from enrichable chunks
                claims_total = 0
                all_claims: list = []
                for row in enrichable:
                    cr = _extract_claims(conn, row["chunk_id"])
                    claims_total += cr.get("claims_extracted", 0)
                    all_claims.extend(cr.get("claims", []))
                results["steps"].append({"claims_extracted": claims_total})

                # Step 3b: Auto-promote high-confidence claims
                promoted = _auto_promote_layer1(conn, all_claims)
                results["steps"].append({"claims_promoted": len(promoted)})
                if promoted:
                    results["promoted_facts"] = [
                        {
                            "subject": p["subject"],
                            "predicate": p["predicate"],
                            "object": p["object"],
                            "fact_id": p["fact_id"],
                        }
                        for p in promoted
                    ]

            if depth == "deep":
                # Step 4: Explain impact for recent facts
                recent = conn.execute(
                    "SELECT fact_id FROM canonical_facts "
                    "WHERE updated_at >= datetime('now', '-7 days') LIMIT 10"
                ).fetchall()
                impacts = []
                for f in recent:
                    imp = _explain_impact(conn, "fact", f["fact_id"])
                    impacts.append(imp.get("total_impacts", 0))
                results["steps"].append({"impacts_analyzed": len(impacts)})

        logger.info("enrich_context: depth=%s, steps=%d", depth, len(results["steps"]))
        return json.dumps(results)
    except Exception as exc:
        logger.error("enrich_context failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool 13: reflect_audit (Phase 0.5 — read-only consolidation candidate audit)
# ═══════════════════════════════════════════════════════════════════════════
@mcp.tool()
def reflect_audit(
    project: str = "",
    stale_days: int = 60,
    abandoned_inbox_days: int = 30,
    limit_per_category: int = 20,
    format: str = "json",
) -> str:
    """Find consolidation candidates without mutating. Read-only, no LLM, no API.

    Detects six categories: exact duplicate task titles, stale overdue
    not_started tasks, notes with empty descriptions, orphan parent_ids,
    abandoned inbox items, and entities without observations. Each candidate
    carries a suggested action so a downstream reviewer can decide
    merge/archive/supersede without re-querying.

    Phase 0.5 of the Reviewable Memory Consolidation pipeline (Ingest →
    Extract → Reconcile → Review → Apply). Subsequent phases will add
    session-source ingestion plus review/apply tooling. MVP excludes
    hard-delete; archive/supersede preserves provenance.

    Args:
        project: filter to a single project (empty = all)
        stale_days: due_date older than this many days counts as stale (default 60)
        abandoned_inbox_days: inbox items untouched this long are flagged (default 30)
        limit_per_category: cap candidates per category (default 20)
        format: "json" (default) or "markdown" (adds rendered report)
    """
    try:
        with _get_conn() as conn:
            report = _audit_reflection_candidates(
                conn,
                project=project or None,
                stale_days=stale_days,
                abandoned_inbox_days=abandoned_inbox_days,
                limit_per_category=limit_per_category,
            )
            logger.info(
                "reflect_audit: total=%d project=%s",
                report["summary"]["total_candidates"],
                project or "*",
            )
            if format == "markdown":
                report["markdown"] = _format_audit_markdown(report)
            return json.dumps(report)
    except Exception as exc:
        logger.error("reflect_audit failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)})


# ── Entry point ──────────────────────────────────────────────────────────
def main() -> None:
    maybe_mount_premium_extensions(mcp, server_name="sqlite-intel")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
