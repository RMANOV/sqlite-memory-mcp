#!/usr/bin/env python3
"""Thin MCP server exposing Intelligence v2 tools.

Shares the same SQLite database as the main sqlite-kb server and keeps the
intelligence surface independently deployable; ``unified_server.py`` also mounts it.
"""

from __future__ import annotations

import json

from fastmcp_compat import FastMCP

from db_utils import (
    get_conn as _get_conn,
    get_conn_immediate as _get_conn_immediate,
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
from reflection_dao import (
    add_candidate as _dao_add_candidate,
    add_input as _dao_add_input,
    archive_run as _dao_archive_run,
    cancel_run as _dao_cancel_run,
    candidate_decision_counts as _dao_candidate_decision_counts,
    create_run as _dao_create_run,
    decide_candidate as _dao_decide_candidate,
    discard_run as _dao_discard_run,
    finish_run as _dao_finish_run,
    get_run as _dao_get_run,
    list_apply_snapshots as _dao_list_apply_snapshots,
    list_candidates as _dao_list_candidates,
    list_inputs as _dao_list_inputs,
    list_runs as _dao_list_runs,
    start_run as _dao_start_run,
    MAX_CANDIDATES_PER_RUN as _MAX_CANDIDATES,
    MAX_INSTRUCTIONS_CHARS as _MAX_INSTRUCTIONS,
    ReflectionStateError as _ReflectionStateError,
    VALID_DECISIONS as _VALID_DECISIONS,
)
from reflection_apply import apply_run as _apply_run
from tools.gbrain_bridge import (
    export_to_gbrain_brain_repo as _export_to_gbrain,
    import_from_gbrain_brain_repo as _import_from_gbrain,
)
from debate import (
    DebateError as _DebateError,
    add_role_to_debate as _debate_add_role_dao,
    advance_watermark as _debate_advance_watermark_dao,
    bind_role_session as _debate_bind_role_session_dao,
    claim_worker_session as _debate_claim_worker_session_dao,
    compact as _debate_compact,
    debate_post_with_recipients as _debate_post_with_recipients_dao,
    debate_signal_advance as _debate_signal_advance_dao,
    debate_signal_check as _debate_signal_check_dao,
    escalate as _debate_escalate_dao,
    get_debate as _debate_get_debate,
    init_debate as _debate_init_dao,
    list_open_debate_work as _debate_list_open_work_dao,
    list_role_bindings as _debate_list_role_bindings_dao,
    post_message as _debate_post_dao,
    prepare_wake_dry_run as _debate_prepare_wake_dry_run_dao,
    read_messages as _debate_read_dao,
    recover_stale_worker_claims as _debate_recover_worker_claims_dao,
    reclaim_stale_message_claims as _debate_reclaim_message_claims_dao,
    reap_worker_claims as _debate_reap_worker_claims_dao,
    rotate_role_binding as _debate_rotate_role_binding_dao,
    seed_initial_role_bindings as _debate_seed_initial_role_bindings_dao,
    set_topic_priority as _debate_set_topic_priority_dao,
    transition_state as _debate_transition_dao,
    validate_topic_id as _debate_validate_topic_id,
    worker_no_action as _debate_worker_no_action_dao,
)
from debate_retrieval import search_debate_context as _search_debate_context
from debate_protocol_v1 import (
    ProtocolV1Error as _ProtocolV1Error,
    get_protocol_state as _debate_protocol_state_dao,
    prepare_order_swap as _debate_judge_prepare_dao,
    record_order_swap_verdict as _debate_judge_verdict_dao,
    sweep_missing_roles as _debate_role_sweep_dao,
    transition_expired_protocols as _debate_protocol_timeout_dao,
    visibility_sql as _debate_visibility_sql,
)
from premium_runtime import (
    evaluate_debate_protocol_creation_gate,
    maybe_mount_premium_extensions,
)

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


# ═══════════════════════════════════════════════════════════════════════════
# Tools 14–19: Phase 1 Reflection lifecycle (reflect_v1.0 — async runs)
# ═══════════════════════════════════════════════════════════════════════════
#
# These tools implement the approved Phase 1 subset of corrections from
# entity MemoryReflection_DreamsAlignmentCorrections (C1, C2, C5, C6, C9,
# C10, C11, C13). reflect_audit (Phase 0.5) remains the read-only audit
# entrypoint; Phase 1 tools wrap it with persistent run state + per-
# candidate human decisions for review/decide workflows.
#
# Note: reflect_start currently runs the Extract stage synchronously
# (mirrors Phase 0.5 audit). Phase 2 will add async background workers
# for LLM-based extraction.


def _reflect_error_response(exc: Exception, *, error_type: str | None = None) -> str:
    """Map exceptions to Dreams-style error envelopes."""
    if isinstance(exc, _ReflectionStateError):
        return json.dumps(
            {"error": str(exc), "error_type": error_type or "internal_error"}
        )
    return json.dumps({"error": str(exc), "error_type": "internal_error"})


# Tool 14: reflect_start
@mcp.tool()
def reflect_start(
    project: str = "",
    stale_days: int = 60,
    abandoned_inbox_days: int = 30,
    limit_per_category: int = 20,
    instructions: str = "",
    model: str = "",
    version: str = "reflect_v1.0",
    created_by: str = "user",
) -> str:
    """Create a Phase 1 run and execute the Extract stage synchronously.

    Reuses Phase 0.5 audit logic (deterministic SQL, no LLM) and persists
    each surfaced candidate to reflection_candidates so it can be reviewed
    via reflect_decide and applied via future reflect_apply.

    Args:
        project: optional project filter for the audit pass.
        stale_days, abandoned_inbox_days, limit_per_category: tuning for the
            Extract stage; same semantics as reflect_audit.
        instructions: free-form guidance text (max 4096 chars per C14/Dreams).
        model: optional model id for future LLM-based runs (Phase 2 uses).
        version: run schema version for forward-compat (default reflect_v1.0).
        created_by: actor recorded in reflection_runs.created_by.
    """
    if instructions and len(instructions) > _MAX_INSTRUCTIONS:
        return json.dumps(
            {
                "error": (
                    f"instructions too long: {len(instructions)} > {_MAX_INSTRUCTIONS}"
                ),
                "error_type": "instructions_too_long",
            }
        )

    run_id: str | None = None
    try:
        with _get_conn() as conn:
            run_id = _dao_create_run(
                conn,
                version=version,
                model=model or None,
                instructions=instructions or None,
                created_by=created_by,
            )
            input_ref = {
                "project": project or None,
                "stale_days": stale_days,
                "abandoned_inbox_days": abandoned_inbox_days,
                "limit_per_category": limit_per_category,
            }
            _dao_add_input(conn, run_id, "tasks", input_ref)
            _dao_start_run(conn, run_id)

            report = _audit_reflection_candidates(
                conn,
                project=project or None,
                stale_days=stale_days,
                abandoned_inbox_days=abandoned_inbox_days,
                limit_per_category=limit_per_category,
            )

            persisted = 0
            for cat_name, items in report["candidates"].items():
                for item in items:
                    target_kind = (
                        "entity" if cat_name == "entities_no_observations" else "task"
                    )
                    target_ref = (
                        item.get("id")
                        or item.get("task_ids", ["?"])[0]
                        or item.get("title_key")
                        or "?"
                    )
                    _dao_add_candidate(
                        conn,
                        run_id,
                        candidate_type=cat_name,
                        suggested_action=item.get("suggested_action", "review"),
                        target_kind=target_kind,
                        target_ref=str(target_ref),
                        evidence=item,
                    )
                    persisted += 1
                    if persisted >= _MAX_CANDIDATES:
                        _dao_finish_run(
                            conn,
                            run_id,
                            "failed",
                            error_type="candidate_limit_exceeded",
                            error_message=(
                                f"persisted {persisted} candidates; cap is "
                                f"{_MAX_CANDIDATES}"
                            ),
                            usage={"candidate_count": persisted},
                        )
                        logger.warning(
                            "reflect_start hit candidate cap: run=%s persisted=%d",
                            run_id,
                            persisted,
                        )
                        return json.dumps(
                            {
                                "run_id": run_id,
                                "status": "failed",
                                "error_type": "candidate_limit_exceeded",
                                "candidates_persisted": persisted,
                            }
                        )

            _dao_finish_run(
                conn,
                run_id,
                "completed",
                usage={
                    "candidate_count": persisted,
                    "categories": report["summary"]["by_category"],
                },
            )
            logger.info(
                "reflect_start: run=%s candidates=%d project=%s",
                run_id,
                persisted,
                project or "*",
            )
            return json.dumps(
                {
                    "run_id": run_id,
                    "status": "completed",
                    "candidates_persisted": persisted,
                    "summary": report["summary"],
                }
            )
    except _ReflectionStateError as exc:
        logger.warning("reflect_start state error: %s", exc)
        return _reflect_error_response(exc)
    except Exception as exc:
        logger.error("reflect_start failed: %s", exc, exc_info=True)
        if run_id is not None:
            try:
                with _get_conn() as conn:
                    _dao_finish_run(
                        conn,
                        run_id,
                        "failed",
                        error_type="internal_error",
                        error_message=str(exc)[:500],
                    )
            except Exception:
                pass
        return _reflect_error_response(exc)


# Tool 15: reflect_status
@mcp.tool()
def reflect_status(run_id: str) -> str:
    """Return run state, inputs, and decision counts for a given run_id."""
    try:
        with _get_conn() as conn:
            row = _dao_get_run(conn, run_id)
            if row is None:
                return json.dumps(
                    {"error": f"run_not_found: {run_id}", "error_type": "not_found"}
                )
            inputs = _dao_list_inputs(conn, run_id)
            counts = _dao_candidate_decision_counts(conn, run_id)
            return json.dumps(
                {"run": row, "inputs": inputs, "candidate_counts": counts}
            )
    except Exception as exc:
        logger.error("reflect_status failed: %s", exc, exc_info=True)
        return _reflect_error_response(exc)


# Tool 16: reflect_history
@mcp.tool()
def reflect_history(
    limit: int = 20,
    offset: int = 0,
    include_archived: bool = False,
    status_filter: str = "",
) -> str:
    """Paginated newest-first list of reflection runs (Dreams list parity)."""
    try:
        with _get_conn() as conn:
            rows, total = _dao_list_runs(
                conn,
                limit=limit,
                offset=offset,
                include_archived=include_archived,
                status_filter=status_filter or None,
            )
            return json.dumps(
                {
                    "runs": rows,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "include_archived": include_archived,
                    "status_filter": status_filter or None,
                }
            )
    except _ReflectionStateError as exc:
        return _reflect_error_response(exc)
    except Exception as exc:
        logger.error("reflect_history failed: %s", exc, exc_info=True)
        return _reflect_error_response(exc)


# Tool 17: reflect_cancel
@mcp.tool()
def reflect_cancel(run_id: str) -> str:
    """Cancel a pending or running run. Rejects terminal states."""
    try:
        with _get_conn() as conn:
            _dao_cancel_run(conn, run_id)
            row = _dao_get_run(conn, run_id)
            return json.dumps(
                {"run_id": run_id, "status": row["status"] if row else None}
            )
    except _ReflectionStateError as exc:
        logger.info("reflect_cancel rejected: %s", exc)
        return json.dumps({"error": str(exc), "error_type": "invalid_state_transition"})
    except Exception as exc:
        logger.error("reflect_cancel failed: %s", exc, exc_info=True)
        return _reflect_error_response(exc)


# Tool 18: reflect_archive
@mcp.tool()
def reflect_archive(run_id: str) -> str:
    """Archive a terminal run. Rejects pending/running. Idempotent."""
    try:
        with _get_conn() as conn:
            changed = _dao_archive_run(conn, run_id)
            row = _dao_get_run(conn, run_id)
            return json.dumps(
                {
                    "run_id": run_id,
                    "archived_at": row["archived_at"] if row else None,
                    "newly_archived": changed,
                }
            )
    except _ReflectionStateError as exc:
        logger.info("reflect_archive rejected: %s", exc)
        return json.dumps({"error": str(exc), "error_type": "invalid_state_transition"})
    except Exception as exc:
        logger.error("reflect_archive failed: %s", exc, exc_info=True)
        return _reflect_error_response(exc)


# Tool 19: reflect_decide
@mcp.tool()
def reflect_decide(
    candidate_id: str,
    decision: str,
    decided_by: str = "user",
) -> str:
    """Record human accept/reject/defer decision on a candidate."""
    if decision not in _VALID_DECISIONS:
        return json.dumps(
            {
                "error": (
                    f"unknown decision: {decision}; expected one of "
                    f"{','.join(_VALID_DECISIONS)}"
                ),
                "error_type": "invalid_argument",
            }
        )
    try:
        with _get_conn() as conn:
            ok = _dao_decide_candidate(conn, candidate_id, decision, decided_by)
            if not ok:
                return json.dumps(
                    {
                        "error": f"candidate_not_found: {candidate_id}",
                        "error_type": "not_found",
                    }
                )
            return json.dumps(
                {
                    "candidate_id": candidate_id,
                    "decision": decision,
                    "decided_by": decided_by,
                }
            )
    except Exception as exc:
        logger.error("reflect_decide failed: %s", exc, exc_info=True)
        return _reflect_error_response(exc)


# ═══════════════════════════════════════════════════════════════════════════
# Tools 20-22: Phase 1 Reflection apply / review / discard
# ═══════════════════════════════════════════════════════════════════════════


# Tool 20: reflect_apply
@mcp.tool()
def reflect_apply(
    run_id: str,
    candidate_ids_csv: str = "",
    applied_by: str = "user",
) -> str:
    """Apply accepted candidates from a completed run.

    Routes mutations through the canonical apply_task_mutation path so
    memory_events and task_field_versions are written consistently.
    Idempotent — candidates that already have a snapshot for this run
    are skipped with reason='already_applied'. Entities are skipped in
    MVP (no entity archive primitive yet).

    Args:
        run_id: id of a run in `completed` status. Other statuses error.
        candidate_ids_csv: optional comma-separated subset of candidate
            ids to apply. Empty string = apply all accepted.
        applied_by: actor recorded on each snapshot row.
    """
    try:
        ids: list[str] | None = None
        if candidate_ids_csv:
            ids = [s.strip() for s in candidate_ids_csv.split(",") if s.strip()]
        with _get_conn() as conn:
            summary = _apply_run(conn, run_id, candidate_ids=ids, applied_by=applied_by)
            logger.info(
                "reflect_apply: run=%s applied=%d skipped=%d failed=%d",
                run_id,
                summary["applied"],
                len(summary["skipped"]),
                len(summary["failed"]),
            )
            return json.dumps(summary)
    except _ReflectionStateError as exc:
        msg = str(exc)
        et = (
            "not_found"
            if msg.startswith("run_not_found")
            else "invalid_state_transition"
        )
        return json.dumps({"error": msg, "error_type": et})
    except Exception as exc:
        logger.error("reflect_apply failed: %s", exc, exc_info=True)
        return _reflect_error_response(exc)


# Tool 21: reflect_review
@mcp.tool()
def reflect_review(
    run_id: str,
    decision_filter: str = "",
    candidate_type_filter: str = "",
    limit: int = 100,
    offset: int = 0,
) -> str:
    """Paginated, filterable list of candidates for human review.

    Args:
        run_id: parent run id.
        decision_filter: empty | pending | accept | reject | defer.
        candidate_type_filter: optional category narrowing
            (e.g. 'stale_overdue_tasks').
        limit: max rows (clamped to 1000).
        offset: pagination cursor.
    """
    try:
        with _get_conn() as conn:
            rows, total = _dao_list_candidates(
                conn,
                run_id,
                decision_filter=decision_filter or None,
                candidate_type_filter=candidate_type_filter or None,
                limit=limit,
                offset=offset,
            )
            apply_snaps, _ = _dao_list_apply_snapshots(conn, run_id=run_id, limit=1000)
            applied_ids = {s["candidate_id"] for s in apply_snaps}
            for r in rows:
                r["already_applied"] = r["candidate_id"] in applied_ids
            return json.dumps(
                {
                    "candidates": rows,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "decision_filter": decision_filter or None,
                    "candidate_type_filter": candidate_type_filter or None,
                }
            )
    except _ReflectionStateError as exc:
        return json.dumps({"error": str(exc), "error_type": "invalid_argument"})
    except Exception as exc:
        logger.error("reflect_review failed: %s", exc, exc_info=True)
        return _reflect_error_response(exc)


# Tool 22: reflect_discard
@mcp.tool()
def reflect_discard(run_id: str) -> str:
    """Hard-delete a terminal run; cascades to inputs/candidates/snapshots.

    Rejects pending/running with invalid_state_transition (cancel first).
    Returns rows_deleted (0 if not found, 1 on success). FK CASCADE
    handles cleanup of dependent rows in a single transaction.
    """
    try:
        with _get_conn() as conn:
            # db_utils configures foreign_keys before opening the transaction;
            # changing this PRAGMA after BEGIN would be a documented no-op.
            rows_deleted = _dao_discard_run(conn, run_id)
            return json.dumps({"run_id": run_id, "rows_deleted": rows_deleted})
    except _ReflectionStateError as exc:
        msg = str(exc)
        et = (
            "not_found"
            if msg.startswith("run_not_found")
            else "invalid_state_transition"
        )
        return json.dumps({"error": msg, "error_type": et})
    except Exception as exc:
        logger.error("reflect_discard failed: %s", exc, exc_info=True)
        return _reflect_error_response(exc)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 23: export_to_gbrain — sqlite-memory-mcp ↔ GBrain bridge (Tier A #4)
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def export_to_gbrain(output_dir: str, project: str = "") -> str:
    """Export entities/observations/relations to GBrain-compatible Markdown.

    Writes one .md file per entity under output_dir/{people,companies,topics}/
    with YAML frontmatter, observations as bullets, and relations as
    relative-path wikilinks. Compatible with the brain-repo layout used by
    Garry Tan's GBrain (github.com/garrytan/gbrain).

    Args:
        output_dir: target folder. Created if missing.
        project: optional project filter; export only entities in that project.
            Empty string exports all entities.

    Returns counters: entities_written, relations_written, observations_written,
    files_written. Deterministic; idempotent overwrite of any existing files.
    """
    try:
        with _get_conn() as conn:
            counts = _export_to_gbrain(
                conn,
                output_dir,
                project_filter=project or None,
            )
            logger.info(
                "export_to_gbrain: dir=%s entities=%d files=%d project=%s",
                output_dir,
                counts["entities_written"],
                counts["files_written"],
                project or "*",
            )
            return json.dumps({"output_dir": output_dir, **counts})
    except OSError as exc:
        logger.error("export_to_gbrain filesystem error: %s", exc)
        return json.dumps({"error": str(exc), "error_type": "filesystem_error"})
    except Exception as exc:
        logger.error("export_to_gbrain failed: %s", exc, exc_info=True)
        return _reflect_error_response(exc)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 24: import_from_gbrain — reverse adapter (Tier A #4 second half)
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def import_from_gbrain(
    input_dir: str,
    project_default: str = "",
    skip_if_exists: bool = True,
) -> str:
    """Import a GBrain-compatible Markdown brain repo into the local KG.

    Walks input_dir/{people,companies,topics}/, parses YAML frontmatter +
    observation bullets + relations as wikilinks, and inserts via the
    canonical entities / observations / relations tables. FTS index is
    refreshed for each touched entity.

    Idempotent: existing entities (matched by UNIQUE name) are skipped by
    default. Pass skip_if_exists=False to re-attempt observations/relations
    insertion against existing rows (entity row itself is never duplicated).

    Args:
        input_dir: folder produced by export_to_gbrain_brain_repo or any
            GBrain-compatible layout. Returns zero counters if missing.
        project_default: project value applied when frontmatter has no
            `project` field. Empty string leaves project NULL.
        skip_if_exists: skip already-known entities (default True).

    Returns counters: entities_created, entities_skipped,
    observations_inserted, relations_inserted, relations_skipped,
    files_parsed, files_skipped.
    """
    try:
        with _get_conn() as conn:
            counts = _import_from_gbrain(
                conn,
                input_dir,
                project_default=project_default or None,
                skip_if_exists=skip_if_exists,
            )
            logger.info(
                "import_from_gbrain: dir=%s entities_created=%d files_parsed=%d",
                input_dir,
                counts["entities_created"],
                counts["files_parsed"],
            )
            return json.dumps({"input_dir": input_dir, **counts})
    except OSError as exc:
        logger.error("import_from_gbrain filesystem error: %s", exc)
        return json.dumps({"error": str(exc), "error_type": "filesystem_error"})
    except Exception as exc:
        logger.error("import_from_gbrain failed: %s", exc, exc_info=True)
        return _reflect_error_response(exc)


# ═══════════════════════════════════════════════════════════════════════════
# Tools 25-30: Debate Protocol v2 — single-channel inter-session coordination
# ═══════════════════════════════════════════════════════════════════════════
# Productized inter-session coordination per CONDUCTOR Tier S #0
# (msg:0a91f237 + 16:35 EEST EXECUTOR INSTRUCTION). Replaces ad-hoc
# observations on a KG entity with a structured channel: 3 tables
# (debates, debate_messages, debate_watermarks), 8-kind enum incl.
# COMPACTION, lifecycle state machine INIT→ACTIVE→RESOLVED→ARCHIVED.


def _debate_error_response(exc: Exception) -> str:
    """Map a DAO exception to a stable MCP error JSON string.

    Per v3.9.2 amendment 7 (msg:e0f47b29):
      - DebateError → emit its ``.error_type`` attribute (specific
        taxonomy or the legacy default ``'debate_validation'``).
      - Any other exception → ``'internal_error'`` (unexpected, NOT a
        validation error). Preserves the v3.9.0+v3.9.1 wire contract:
        return type stays ``str`` via ``json.dumps``.
    """
    if isinstance(exc, _DebateError):
        payload = {"error": str(exc), "error_type": exc.error_type}
        if getattr(exc, "details", None):
            payload["details"] = exc.details
        return json.dumps(payload)
    if isinstance(exc, _ProtocolV1Error):
        payload = {"error": str(exc), "error_type": exc.error_type}
        if exc.details:
            payload["details"] = exc.details
        return json.dumps(payload)
    return json.dumps({"error": str(exc), "error_type": "internal_error"})


def _debate_gate_denied_response(verdict: dict[str, object]) -> str:
    reason = str(verdict.get("reason") or "premium_gate_denied")
    return json.dumps(
        {
            "error": f"debate_protocol_gate_denied: {reason}",
            "error_type": "premium_gate_denied",
            "gate": verdict,
        }
    )


def _debate_topic_exists(conn, topic_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM debates WHERE topic_id = ? LIMIT 1",
        (topic_id,),
    ).fetchone()
    return row is not None


# Tool 25: debate_init
@mcp.tool()
def debate_init(
    topic_id: str,
    title: str,
    roles_json: str,
    created_by_role: str,
    resolve_by: str = "",
    metadata_json: str = "",
    protocol_version: str = "",
    blind_roles_csv: str = "",
    max_rounds: int = 3,
    phase_timeout_seconds: int = 300,
) -> str:
    """Bootstrap a new debate. Idempotent on (topic_id, roles).

    Args:
        topic_id: matches ^[A-Z][A-Z0-9_]+$.
        title: non-empty.
        roles_json: JSON array of {role, session_id} dicts.
        created_by_role: role posting the init.
        resolve_by: optional ISO 8601 UTC deadline.
        metadata_json: JSON object. New official topics must include either
            conductor_priority.lane or priority_lane (P0..P7) plus a priority
            reason, so human-entered topics are asked for priority or
            CONDUCTOR assesses priority before creation.
        protocol_version: empty for legacy behavior, or ``debate/v1`` for
            deterministic §7 server semantics.
        blind_roles_csv: exactly two declared semantic roles when using
            debate/v1; their first CLAIMs are mutually hidden until committed.
    """
    try:
        roles = json.loads(roles_json) if roles_json else []
        metadata = json.loads(metadata_json) if metadata_json else None
        blind_roles = [r.strip() for r in blind_roles_csv.split(",") if r.strip()]
        with _get_conn_immediate() as conn:
            if not _debate_topic_exists(conn, topic_id):
                gate_verdict = evaluate_debate_protocol_creation_gate(
                    conn,
                    server_name="sqlite-intel",
                    tool_name="sqlite-intel.debate_init",
                    actor_id=created_by_role,
                    payload={
                        "topic_id": topic_id,
                        "created_by_role": created_by_role,
                    },
                )
                if not gate_verdict.get("allowed"):
                    logger.info(
                        "debate_init premium gate denied: topic=%s reason=%s",
                        topic_id,
                        gate_verdict.get("reason"),
                    )
                    return _debate_gate_denied_response(gate_verdict)
            out = _debate_init_dao(
                conn,
                topic_id=topic_id,
                title=title,
                roles=roles,
                created_by_role=created_by_role,
                resolve_by=resolve_by or None,
                metadata=metadata,
                require_priority=True,
                protocol_version=protocol_version or None,
                blind_roles=blind_roles if protocol_version else None,
                max_rounds=max_rounds,
                phase_timeout_seconds=phase_timeout_seconds,
            )
            seeded_bindings = _debate_seed_initial_role_bindings_dao(
                conn,
                topic_id=topic_id,
                roles=roles,
                bound_by_role=created_by_role,
                reason="debate_init seeded active role binding",
            )
            out["seeded_bindings"] = seeded_bindings
            logger.info(
                "debate_init: topic=%s state=%s roles=%d seeded_bindings=%d",
                out["topic_id"],
                out["state"],
                len(out["roles"]),
                len(seeded_bindings),
            )
            return json.dumps(out)
    except _DebateError as exc:
        logger.info("debate_init rejected: %s", exc)
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_init failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


def _signal_wake_after_commit() -> None:
    """Windows-only latency hint for the resident debate pump.

    Fires strictly AFTER the write transaction has committed (callers invoke
    it outside the connection context manager). The named event is only a
    hint — the durable outbox is the message/recipient/wake rows themselves —
    so any failure here must never surface into the post result.
    """
    import sys as _sys

    if _sys.platform != "win32":
        return
    try:
        from debate_wake_signal import signal_wake

        signal_wake()
    except Exception:  # noqa: BLE001 — a failed hint must never fail the post
        logger.debug("debate wake signal failed", exc_info=True)


# Tool 26: debate_post
@mcp.tool()
def debate_post(
    topic_id: str,
    role: str,
    priority: str,
    kind: str,
    body: str,
    reply_to: str = "",
    standing: bool | None = None,
    vehicle: str = "",
    protocol_version: str = "",
    body_mode: str = "",
    payload_json: str = "",
    author_session_id: str = "",
) -> str:
    """Append a message to a debate. Validates kind-specific semantics
    BEFORE the INSERT (atomic — failed validation leaves no row).

    Args:
        topic_id: existing debate topic.
        role: must appear in declared roles.
        priority: H | M | L | INFO.
        kind: legacy kind, or CLAIM | CHALLENGE | EVIDENCE | REBUT |
            CONCEDE | VERIFY | DISSENT | ESCALATE for debate/v1.
        body: non-empty.
        reply_to: optional msg_id in same topic.
        vehicle: optional work classification — analysis | review |
            implementation. Empty/absent → analysis (backcompat). Gates the
            wake/pump router: implementation-tagged work fails closed instead
            of dispatching a no-edit wake-worker (routed to a
            conductor-approved impl vehicle out-of-band).
    """
    try:
        with _get_conn_immediate() as conn:
            out = _debate_post_dao(
                conn,
                topic_id=topic_id,
                role=role,
                priority=priority,
                kind=kind,
                body=body,
                reply_to=reply_to or None,
                standing=standing,
                vehicle=vehicle or None,
                protocol_version=protocol_version or None,
                body_mode=body_mode or None,
                payload_json=payload_json or None,
                author_session_id=author_session_id or None,
            )
        # Transaction committed on context exit; only now hint the pump.
        _signal_wake_after_commit()
        return json.dumps(out)
    except _DebateError as exc:
        logger.info("debate_post rejected: %s", exc)
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_post failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 27: debate_read
@mcp.tool()
def debate_read(
    topic_id: str,
    role: str,
    since_msg_id: str = "",
    since_ts: str = "",
    since_latest_compaction: bool = False,
    kind_filter_csv: str = "",
    priority_filter_csv: str = "",
    limit: int = 200,
) -> str:
    """Read messages with compound (ts, msg_id) cursor.

    Cursor priority (turn-3 fix):
      1. since_latest_compaction=True → use latest COMPACTION's
         (ts, msg_id) as cursor; bootstrap_compaction_msg_id field set.
         Falls through to existing precedence if no COMPACTION exists.
      2. since_msg_id (raises unknown_since_msg_id if not in topic).
      3. since_ts.
      4. role watermark.
      5. start of topic.

    Default limit=200, cap=1000. Returns truncated + next cursors when
    more messages remain.
    """
    try:
        kind_filter = (
            [k.strip() for k in kind_filter_csv.split(",") if k.strip()]
            if kind_filter_csv
            else None
        )
        priority_filter = (
            [p.strip() for p in priority_filter_csv.split(",") if p.strip()]
            if priority_filter_csv
            else None
        )
        with _get_conn() as conn:
            out = _debate_read_dao(
                conn,
                topic_id=topic_id,
                role=role,
                since_msg_id=since_msg_id or None,
                since_ts=since_ts or None,
                since_latest_compaction=since_latest_compaction,
                kind_filter=kind_filter,
                priority_filter=priority_filter,
                limit=limit,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_read failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 27b: debate_search (B5 — read-only LIKE-over-body, Option A)
@mcp.tool()
def debate_search(
    topic_id: str, query: str, limit: int = 50, viewer_role: str = ""
) -> str:
    """Search a debate topic's messages by substring of ``body``.

    Read-only, parameterized ``LIKE`` over the message body, scoped to a
    single ``topic_id``. The ``query`` is matched LITERALLY: its SQL LIKE
    wildcards (``%``, ``_``) and the escape char (``\\``) are escaped before
    being wrapped as ``%<query>%``, so user input can never inject wildcard
    or pattern semantics. Returns the same per-message column shape as
    ``debate_read`` (msg_id, topic_id, role, ts, priority, kind, reply_to,
    standing, body, created_at), newest first.

    Args:
        topic_id: existing debate topic to search within.
        query: literal substring to find in message bodies. An empty
            string matches every message in the topic.
        limit: max rows to return; clamped to 1..500 (non-int/<=0 → 1).

    Returns:
        JSON dict ``{"topic_id", "query", "count", "limit", "messages"}``.
    """
    try:
        # Clamp limit defensively to 1..500 (treat <=0 / non-int as 1).
        if not isinstance(limit, int) or limit < 1:
            effective_limit = 1
        else:
            effective_limit = min(limit, 500)

        # Escape LIKE wildcards so the query is matched literally. Order
        # matters: escape the escape char FIRST, then the wildcards.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"

        _debate_validate_topic_id(topic_id)
        with _get_conn() as conn:
            if _debate_get_debate(conn, topic_id) is None:
                raise _DebateError(f"unknown_topic: {topic_id}")
            visibility, visibility_params = _debate_visibility_sql(
                alias="m", viewer_role=viewer_role, control_plane=False
            )
            rows = conn.execute(
                "SELECT m.msg_id, m.topic_id, m.role, m.ts, m.priority, m.kind, "
                "m.reply_to, m.standing, m.body, m.protocol_version, m.round_no, "
                "m.body_mode, m.payload_json, m.created_at FROM debate_messages m "
                "WHERE m.topic_id = ? AND "
                + visibility
                + " AND m.body LIKE ? ESCAPE '\\' "
                "ORDER BY m.ts DESC, m.msg_id DESC LIMIT ?",
                (topic_id, *visibility_params, pattern, effective_limit),
            ).fetchall()
            messages = []
            for row in rows:
                item = dict(row)
                if item.get("protocol_version") is None:
                    for key in (
                        "protocol_version",
                        "round_no",
                        "body_mode",
                        "payload_json",
                    ):
                        item.pop(key, None)
                messages.append(item)
            return json.dumps(
                {
                    "topic_id": topic_id,
                    "query": query,
                    "count": len(messages),
                    "limit": effective_limit,
                    "messages": messages,
                }
            )
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_search failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 27c: bounded hybrid debate retrieval (FTS5 BM25 + literal/metadata)
@mcp.tool()
def debate_context_search(
    topic_id: str,
    query: str,
    limit: int = 10,
    role: str = "",
    session_id: str = "",
) -> str:
    """Rank relevant debate context without dumping full message bodies.

    Runs two native-memory.db retrieval paths: FTS5/BM25 token search and a
    Unicode literal/metadata search.  Results are merged with weighted RRF,
    then re-ranked by direct recipient, priority, kind, recency, unresolved-Q,
    active-topic, and body-length signals.  Every result contains a bounded
    snippet plus source ranks and score receipts; the full body is omitted.
    """
    try:
        _debate_validate_topic_id(topic_id)
        with _get_conn() as conn:
            # Read-only equivalent for the retrieval call.  Schema migration
            # happens before this wrapper opens; the ranked search itself can
            # execute no writes even if a future helper accidentally tries.
            conn.execute("PRAGMA query_only=ON")
            if _debate_get_debate(conn, topic_id) is None:
                raise _DebateError(f"unknown_topic: {topic_id}")
            out = _search_debate_context(
                conn,
                query=query,
                topic_ids=[topic_id],
                target_role=role,
                target_session_id=session_id,
                limit=limit,
            )
            out["topic_id"] = topic_id
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_context_search failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# debate/v1 §7 control-plane tools.  They expose deterministic projections;
# they do not ask an agent to infer state from prose.
@mcp.tool()
def debate_protocol_state(topic_id: str) -> str:
    """Return the debate/v1 micro-state for one topic."""
    try:
        _debate_validate_topic_id(topic_id)
        with _get_conn() as conn:
            state = _debate_protocol_state_dao(conn, topic_id)
            if state is None:
                raise _DebateError(
                    f"protocol_not_configured: {topic_id}",
                    error_type="PROTOCOL_NOT_CONFIGURED",
                )
            return json.dumps(state)
    except Exception as exc:
        return _debate_error_response(exc)


@mcp.tool()
def debate_judge_prepare(topic_id: str, left_msg_id: str, right_msg_id: str) -> str:
    """Create immutable AB and BA judge projections for adjudication."""
    try:
        with _get_conn_immediate() as conn:
            out = _debate_judge_prepare_dao(
                conn,
                topic_id=topic_id,
                left_msg_id=left_msg_id,
                right_msg_id=right_msg_id,
            )
        return json.dumps(out)
    except Exception as exc:
        return _debate_error_response(exc)


@mcp.tool()
def debate_judge_verdict(projection_id: str, judge_role: str, verdict_json: str) -> str:
    """Record one immutable judge verdict; AB/BA agreement stops the debate."""
    try:
        verdict = json.loads(verdict_json)
        with _get_conn_immediate() as conn:
            out = _debate_judge_verdict_dao(
                conn,
                projection_id=projection_id,
                judge_role=judge_role,
                verdict=verdict,
            )
        return json.dumps(out)
    except Exception as exc:
        return _debate_error_response(exc)


@mcp.tool()
def debate_protocol_maintain(topic_ids_csv: str = "") -> str:
    """Run deterministic phase-timeout and missing-role recovery sweeps."""
    try:
        topic_ids = [v.strip() for v in topic_ids_csv.split(",") if v.strip()]
        for topic_id in topic_ids:
            _debate_validate_topic_id(topic_id)
        with _get_conn_immediate() as conn:
            timed_out = _debate_protocol_timeout_dao(conn)
            recovered = _debate_role_sweep_dao(conn, topic_ids=topic_ids)
        if recovered:
            _signal_wake_after_commit()
        return json.dumps({"timed_out": timed_out, "role_recoveries": recovered})
    except Exception as exc:
        return _debate_error_response(exc)


# Tool 28: debate_state
@mcp.tool()
def debate_state(
    topic_id: str,
    role: str,
    new_state: str,
    reason: str = "",
) -> str:
    """Transition debate to a new state. Validates VALID_TRANSITIONS.
    RESOLVED requires all open Qs to have a matching A reply (or A body
    starting `[DEFERRED:` to count as resolution-equivalent).
    """
    try:
        with _get_conn_immediate() as conn:
            out = _debate_transition_dao(
                conn,
                topic_id=topic_id,
                role=role,
                new_state=new_state,
                reason=reason or "",
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_state failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 29: debate_escalate
@mcp.tool()
def debate_escalate(
    topic_id: str,
    role: str,
    reason: str,
    target_role: str = "HUMAN",
) -> str:
    """Force-write an H-priority PING tagged for target_role (default HUMAN)."""
    try:
        with _get_conn() as conn:
            out = _debate_escalate_dao(
                conn,
                topic_id=topic_id,
                role=role,
                reason=reason,
                target_role=target_role,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_escalate failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 30: debate_compact
@mcp.tool()
def debate_compact(
    topic_id: str,
    role: str,
    body: str,
    since_ts: str = "",
    until_ts: str = "",
) -> str:
    """Write a COMPACTION snapshot. Body must contain OBSERVE / ORIENT /
    DECIDE / ACT sections (regex-validated pre-INSERT). since_ts and
    until_ts are optional ISO 8601 UTC bounds for the snapshotted range.
    """
    try:
        with _get_conn() as conn:
            out = _debate_compact(
                conn,
                topic_id=topic_id,
                role=role,
                body=body,
                since_ts=since_ts or None,
                until_ts=until_ts or None,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_compact failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 31: debate_advance_watermark (turn-3 helper, msg:4c8a91be)
@mcp.tool()
def debate_advance_watermark(
    topic_id: str,
    role: str,
    processed_up_to_msg_id: str,
) -> str:
    """Advance (topic_id, role) cursor to a specific msg_id.

    Convenience wrapper that looks up the msg_id's ts and writes a
    canonical WATERMARK message of the form:
      'processed_up_to_ts=<ts> processed_up_to_msg_id=<msg_id>'

    Atomically updates debate_watermarks. Reduces caller error surface
    vs constructing the WATERMARK body by hand.
    """
    try:
        with _get_conn() as conn:
            out = _debate_advance_watermark_dao(
                conn,
                topic_id=topic_id,
                role=role,
                processed_up_to_msg_id=processed_up_to_msg_id,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_advance_watermark failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 32: debate_post_with_recipients (v3.9.2 prompt-time inbox signaling)
@mcp.tool()
def debate_post_with_recipients(
    topic_id: str,
    role: str,
    priority: str,
    kind: str,
    body: str,
    addressed_to_csv: str,
    diagnostic_to_csv: str = "",
    conductor_override_msg_id: str = "",
    reply_to: str = "",
    standing: bool | None = None,
    vehicle: str = "",
    protocol_version: str = "",
    body_mode: str = "",
    payload_json: str = "",
    author_session_id: str = "",
) -> str:
    """Post an addressed message: debate_messages + debate_message_recipients
    in a single atomic transaction.

    vehicle: optional work classification — analysis | review |
    implementation. Empty/absent → analysis (backcompat). implementation-
    tagged messages FAIL CLOSED at the wake/pump router (no bounded no-edit
    wake-worker is spawned); they are routed to a conductor-approved impl
    vehicle out-of-band.

    addressed_to_csv: comma-separated list of recipients. Each entry must
    be either a declared role of the topic OR a session_id with an
    approved runtime prefix (cc-, codex-, mcp-, tray-, human-) and a
    [a-zA-Z0-9_]{4,64} suffix. Empty list rejected (broadcast not
    supported in v3.9.x). Duplicates silently de-duplicated preserving
    order. ARCHIVED topics block all kinds (including STATE);
    RESOLVED topics block all non-STATE kinds.
    """
    try:
        addressed_to = [r.strip() for r in addressed_to_csv.split(",") if r.strip()]
        diagnostic_to = [r.strip() for r in diagnostic_to_csv.split(",") if r.strip()]
        # v3.9.3: BEGIN IMMEDIATE wrapper — write path requires
        # serialized reads + writes against other writers (msg:34adcb3e
        # amendment 1A). Race-safety contract is wrapper-scoped.
        with _get_conn_immediate() as conn:
            out = _debate_post_with_recipients_dao(
                conn,
                topic_id=topic_id,
                role=role,
                priority=priority,
                kind=kind,
                body=body,
                addressed_to=addressed_to,
                diagnostic_to=diagnostic_to,
                conductor_override_msg_id=conductor_override_msg_id or None,
                reply_to=reply_to or None,
                standing=standing,
                vehicle=vehicle or None,
                protocol_version=protocol_version or None,
                body_mode=body_mode or None,
                payload_json=payload_json or None,
                author_session_id=author_session_id or None,
            )
        # Transaction committed on context exit; only now hint the pump.
        _signal_wake_after_commit()
        return json.dumps(out)
    except _DebateError as exc:
        logger.info("debate_post_with_recipients rejected: %s", exc)
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_post_with_recipients failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 33: debate_signal_check (v3.9.2 prompt-time inbox poll)
@mcp.tool()
def debate_signal_check(
    session_id: str,
    role: str,
    topic_id: str,
    since_msg_id: str = "",
    since_ts: str = "",
    limit: int = 200,
) -> str:
    """Return messages addressed to (role OR session_id) past the
    caller's compound (ts, msg_id) cursor.

    session_id must match ^(cc|codex|mcp|tray|human)-[a-zA-Z0-9_]{4,64}$.
    Cursor precedence: since_msg_id > since_ts > debate_signal_state row
    > start of topic. limit defaults to 200, capped at 1000; out-of-range
    or non-int raises with a specific error_type. Returns pending list,
    truncated bool + next_cursor for pagination, max_priority for
    short-circuit logic, plus topic_state.
    """
    try:
        # v3.11: non-standing DECISION delivery performs atomic one-shot
        # claim writes, so signal_check now needs BEGIN IMMEDIATE.
        with _get_conn_immediate() as conn:
            out = _debate_signal_check_dao(
                conn,
                session_id=session_id,
                role=role,
                topic_id=topic_id,
                since_msg_id=since_msg_id or None,
                since_ts=since_ts or None,
                limit=limit,
            )
            return json.dumps(out)
    except _DebateError as exc:
        logger.info("debate_signal_check rejected: %s", exc)
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_signal_check failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 34: debate_signal_advance (v3.9.2 cursor advance with recipient guard)
@mcp.tool()
def debate_signal_advance(
    session_id: str,
    role: str,
    topic_id: str,
    last_processed_msg_id: str,
) -> str:
    """Advance the (session_id, role, topic_id) compound cursor to a
    specific msg_id.

    The target msg_id MUST be addressed to the caller (role OR
    session_id) — turn-12 fix: prevents a buggy adapter from advancing
    past unprocessed addressed work and permanently hiding pending
    messages. ts is derived from the message row (not caller-supplied).
    Atomic upsert into debate_signal_state writes BOTH cursor columns
    plus last_check_at.
    """
    try:
        # v3.9.3: BEGIN IMMEDIATE wrapper — advance has the same
        # read-then-write race as post_with_recipients
        # (msg:34adcb3e amendment 1A).
        with _get_conn_immediate() as conn:
            out = _debate_signal_advance_dao(
                conn,
                session_id=session_id,
                role=role,
                topic_id=topic_id,
                last_processed_msg_id=last_processed_msg_id,
            )
            return json.dumps(out)
    except _DebateError as exc:
        logger.info("debate_signal_advance rejected: %s", exc)
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_signal_advance failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 35: debate_binding_list (v3.10 role/session lifecycle)
@mcp.tool()
def debate_binding_list(topic_id: str) -> str:
    """List role/session bindings and cursor state for a debate topic."""
    try:
        with _get_conn() as conn:
            return json.dumps(_debate_list_role_bindings_dao(conn, topic_id=topic_id))
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_binding_list failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 36: debate_bind_role (v3.10 role/session lifecycle)
@mcp.tool()
def debate_bind_role(
    topic_id: str,
    role: str,
    session_id: str,
    runtime: str = "",
    state: str = "active",
    reason: str = "",
    bound_by_role: str = "",
    bound_by_msg_id: str = "",
    replace_active: bool = False,
    conductor_override_msg_id: str = "",
) -> str:
    """Bind, diagnose, or retire a role/session binding.

    Direct retirement of an active owner requires a CONDUCTOR override
    DECISION msg_id. Duplicate active primary owners are rejected unless
    replace_active is explicitly set for an atomic swap.
    """
    try:
        with _get_conn_immediate() as conn:
            out = _debate_bind_role_session_dao(
                conn,
                topic_id=topic_id,
                role=role,
                session_id=session_id,
                runtime=runtime,
                state=state,
                reason=reason,
                bound_by_role=bound_by_role or None,
                bound_by_msg_id=bound_by_msg_id or None,
                replace_active=replace_active,
                conductor_override_msg_id=conductor_override_msg_id or None,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_bind_role failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 36b: debate_add_role (flexible roster — add a role after debate_init)
@mcp.tool()
def debate_add_role(
    topic_id: str,
    role: str,
    session_id: str,
    runtime: str = "",
    reason: str = "",
    bound_by_role: str = "",
    bound_by_msg_id: str = "",
    replace_active: bool = False,
    conductor_override_msg_id: str = "",
) -> str:
    """Add a NEW role to an existing topic after debate_init (flexible roster).

    Roles are no longer frozen at debate_init. This appends ``role`` to the
    declared roster and installs an active primary binding in one transaction,
    so messages can be addressed to the role immediately. Idempotent: if the
    role is already declared and this session already owns it, returns the
    existing binding with ``added_role=False``.

    To swap a different active owner for this session, set ``replace_active``
    (atomic). To preserve the new owner's read cursor on an exhausted-session
    handoff, prefer ``debate_rotate_binding`` instead.
    """
    try:
        with _get_conn_immediate() as conn:
            out = _debate_add_role_dao(
                conn,
                topic_id=topic_id,
                role=role,
                session_id=session_id,
                runtime=runtime,
                reason=reason or "debate_add_role flexible roster",
                bound_by_role=bound_by_role or None,
                bound_by_msg_id=bound_by_msg_id or None,
                replace_active=replace_active,
                conductor_override_msg_id=conductor_override_msg_id or None,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_add_role failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 37: debate_rotate_binding (v3.10 role/session lifecycle)
@mcp.tool()
def debate_rotate_binding(
    topic_id: str,
    role: str,
    old_session_id: str,
    new_session_id: str,
    cursor_mode: str,
    runtime: str = "",
    reason: str = "",
    bound_by_role: str = "",
    bound_by_msg_id: str = "",
) -> str:
    """Atomically rotate a role owner with explicit cursor mode.

    cursor_mode must be head, copy, or replay. Missing/invalid mode fails.
    """
    try:
        with _get_conn_immediate() as conn:
            out = _debate_rotate_role_binding_dao(
                conn,
                topic_id=topic_id,
                role=role,
                old_session_id=old_session_id,
                new_session_id=new_session_id,
                runtime=runtime,
                cursor_mode=cursor_mode,
                reason=reason,
                bound_by_role=bound_by_role or None,
                bound_by_msg_id=bound_by_msg_id or None,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_rotate_binding failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 38: debate_close_topic (v3.10 close helper)
@mcp.tool()
def debate_close_topic(
    topic_id: str, role: str, new_state: str, reason: str = ""
) -> str:
    """Close a topic through the authoritative debate_state transition path.

    Binding retirement happens in the same transaction as RESOLVED/ARCHIVED.
    """
    try:
        with _get_conn_immediate() as conn:
            out = _debate_transition_dao(
                conn,
                topic_id=topic_id,
                role=role,
                new_state=new_state,
                reason=reason or "",
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_close_topic failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 39: debate_wake_dry_run (v3.10 PostToolUse dry-run)
@mcp.tool()
def debate_wake_dry_run(tool_response_json: str, action: str = "dry_run_wake") -> str:
    """Resolve wake targets and audit them without waking or posting.

    Unknown tool_response schema fails closed and writes a schema_mismatch
    audit row. Real wake actions are intentionally out of scope.
    """
    try:
        tool_response = json.loads(tool_response_json) if tool_response_json else {}
        with _get_conn_immediate() as conn:
            out = _debate_prepare_wake_dry_run_dao(
                conn,
                tool_response=tool_response,
                action=action or "dry_run_wake",
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_wake_dry_run failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 40: debate_worker_claim (v3.11 wake worker identity)
@mcp.tool()
def debate_worker_claim(
    topic_id: str,
    role: str,
    parent_session_id: str,
    trigger_msg_id: str,
    details_json: str = "",
) -> str:
    """Idempotently allocate/reuse a derived -W<n> worker for one trigger."""
    try:
        details = json.loads(details_json) if details_json else None
        with _get_conn_immediate() as conn:
            out = _debate_claim_worker_session_dao(
                conn,
                topic_id=topic_id,
                role=role,
                parent_session_id=parent_session_id,
                trigger_msg_id=trigger_msg_id,
                details=details,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_worker_claim failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 41: debate_worker_reap (v3.11 worker retention cleanup)
@mcp.tool()
def debate_worker_reap(topic_id: str, older_than_ts: str) -> str:
    """Retire old completed worker claims and leave audit evidence."""
    try:
        with _get_conn_immediate() as conn:
            out = _debate_reap_worker_claims_dao(
                conn,
                topic_id=topic_id,
                older_than_ts=older_than_ts,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_worker_reap failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 42: debate_message_claim_reclaim (v3.11.2 one-shot DECISION recovery)
@mcp.tool()
def debate_message_claim_reclaim(
    topic_id: str,
    older_than_ts: str,
    minimum_age_seconds: int = 60,
) -> str:
    """Reclaim stale active standing=false DECISION claims with audit rows."""
    try:
        with _get_conn_immediate() as conn:
            out = _debate_reclaim_message_claims_dao(
                conn,
                topic_id=topic_id,
                older_than_ts=older_than_ts,
                minimum_age_seconds=minimum_age_seconds,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_message_claim_reclaim failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 43: debate_worker_no_action (v3.11.4 no-op worker completion)
@mcp.tool()
def debate_worker_no_action(
    topic_id: str,
    role: str,
    worker_session_id: str,
    trigger_msg_id: str,
    reason: str = "",
) -> str:
    """Complete a wake worker claim without posting when no work remains."""
    try:
        with _get_conn_immediate() as conn:
            out = _debate_worker_no_action_dao(
                conn,
                topic_id=topic_id,
                role=role,
                worker_session_id=worker_session_id,
                trigger_msg_id=trigger_msg_id,
                reason=reason,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_worker_no_action failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 43b: reconcile workers whose launcher process exited without a receipt
@mcp.tool()
def debate_worker_recover_stale(
    topic_id: str,
    older_than_ts: str,
    minimum_age_seconds: int = 120,
) -> str:
    """Retire dead worker claims without hiding their parent trigger.

    This manual surface assumes no worker in the selected stale set is live;
    the resident pump performs the same DAO operation with a live-PID allow
    list.  A matching terminal A/STATUS completes the claim; otherwise it is
    retired while the parent cursor remains unchanged.
    """
    try:
        with _get_conn_immediate() as conn:
            out = _debate_recover_worker_claims_dao(
                conn,
                topic_id=topic_id,
                older_than_ts=older_than_ts,
                minimum_age_seconds=minimum_age_seconds,
                live_worker_session_ids=set(),
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_worker_recover_stale failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 44: debate_set_topic_priority (CONDUCTOR cross-topic scheduling)
@mcp.tool()
def debate_set_topic_priority(
    topic_id: str,
    role: str,
    lane: str,
    reason: str,
    next_action: str = "",
    blocked_by: str = "",
) -> str:
    """Set a CONDUCTOR-owned P0..P7 priority lane in topic metadata."""
    try:
        with _get_conn_immediate() as conn:
            out = _debate_set_topic_priority_dao(
                conn,
                topic_id=topic_id,
                role=role,
                lane=lane,
                reason=reason,
                next_action=next_action,
                blocked_by=blocked_by,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_set_topic_priority failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# Tool 45: debate_work_queue (deterministic open-work priority view)
@mcp.tool()
def debate_work_queue(
    states_csv: str = "INIT,ACTIVE",
    topics_csv: str = "",
    limit: int = 50,
) -> str:
    """List open debate topics in deterministic CONDUCTOR priority order."""
    try:
        states = [s.strip() for s in states_csv.split(",") if s.strip()]
        topics = [t.strip() for t in topics_csv.split(",") if t.strip()]
        with _get_conn() as conn:
            out = _debate_list_open_work_dao(
                conn,
                states=states or None,
                topics=topics or None,
                limit=limit,
            )
            return json.dumps(out)
    except _DebateError as exc:
        return _debate_error_response(exc)
    except Exception as exc:
        logger.error("debate_work_queue failed: %s", exc, exc_info=True)
        return _debate_error_response(exc)


# ── Entry point ──────────────────────────────────────────────────────────
def main() -> None:
    maybe_mount_premium_extensions(mcp, server_name="sqlite-intel")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
