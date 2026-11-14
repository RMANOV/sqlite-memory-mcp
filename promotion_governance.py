"""Snapshot-bound, one-use operator approval for canonical promotion.

The CLI is a deliberate approval channel separate from MCP. It is not OS
authentication of a human and cannot defend against arbitrary same-UID SQL or
Python. Receipts are local only; replication must never import them.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any
import unicodedata
import uuid

CONTRACT_VERSION = "operator-promotion-v1"
APPROVAL_TTL_SECONDS = 900
ALLOWED_STATES = frozenset({"enrichable", "uncertain", "awaiting_human"})
ALLOWED_ENRICH_POLICIES = frozenset({"manual", "auto", "ask"})


class ApprovalError(ValueError):
    """The reviewed snapshot or approval is not eligible for promotion."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _savepoint(conn: sqlite3.Connection):
    name = "promotion_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
        conn.execute(f"RELEASE SAVEPOINT {name}")
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise


def _one(conn, sql, args, label):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise ApprovalError(f"Missing {label}")
    return dict(row)


def _rows(conn, sql, args):
    return [dict(row) for row in conn.execute(sql, args)]


def _policy():
    from intelligence_v2 import load_config
    config = load_config()
    fields = ("enabled", "respect_signal_phrases", "human_required_scopes", "default_enrich_policy")
    result = {name: config.get(name) for name in fields}
    if result["enabled"] is not True:
        raise ApprovalError("Intelligence promotion is disabled")
    scopes = result["human_required_scopes"]
    if not isinstance(scopes, list) or not all(isinstance(x, str) for x in scopes):
        raise ApprovalError("Invalid promotion scope policy")
    result["human_required_scopes"] = sorted(set(scopes))
    result.update(contract_version=CONTRACT_VERSION, ttl_seconds=APPROVAL_TTL_SECONDS)
    return result


def _chunk_source(conn, chunk_id):
    row = _one(conn, "SELECT * FROM context_chunks WHERE chunk_id=?", (chunk_id,), "source chunk")
    if row["state"] not in ALLOWED_STATES:
        raise ApprovalError(f"Source state {row['state']!r} cannot be promoted")
    if row["enrich_policy"] not in ALLOWED_ENRICH_POLICIES:
        raise ApprovalError("Source enrichment policy does not permit review")
    kind, ref = row["source_type"], row["source_ref"]
    if kind in {"text", "manual"}:
        origin = {"kind": "self_contained_text"}
    elif kind == "observation":
        origin = _observation_source(conn, ref)
        observation = origin["observation"]
        if row["entity_id"] is not None and str(row["entity_id"]) != str(observation["entity_id"]):
            raise ApprovalError("Source chunk observation belongs to another entity")
        if row["body"] != observation["content"]:
            raise ApprovalError("Source observation changed or chunk is not its current content")
    elif kind in {"task", "note"}:
        task = _one(conn, "SELECT * FROM tasks WHERE id=?", (ref,), "source task/note")
        if task["status"] in {"archived", "cancelled"}:
            raise ApprovalError("Source task/note is archived or cancelled")
        if kind == "note" and task["type"] != "note":
            raise ApprovalError("Source note has changed type")
        origin = {"task": task}
    elif kind == "entity":
        origin = _evidence_source(conn, "entity", ref)
    else:
        raise ApprovalError(f"Unsupported primary source lineage {kind!r}")
    return {
        "chunk": row,
        "current_origin": origin,
        "annotations": _rows(conn, "SELECT * FROM context_annotations WHERE chunk_id=? ORDER BY annotation_id", (chunk_id,)),
        "questions": _rows(conn, "SELECT * FROM context_questions WHERE chunk_id=? ORDER BY question_id", (chunk_id,)),
    }


def _observation_source(conn, observation_id):
    observation = _one(conn, "SELECT * FROM observations WHERE id=?", (observation_id,), "source observation")
    entity = _one(conn, "SELECT * FROM entities WHERE id=?", (observation["entity_id"],), "source entity")
    return {"observation": observation, "entity": entity}


def _evidence_source(conn, kind, ref):
    if kind in {"chunk", "source_chunk"}:
        return _chunk_source(conn, ref)
    if kind == "observation":
        return _observation_source(conn, ref)
    if kind == "entity":
        entity = _one(conn, "SELECT * FROM entities WHERE id=?", (ref,), "evidence entity")
        return {"entity": entity, "observations": _rows(conn, "SELECT * FROM observations WHERE entity_id=? ORDER BY id", (ref,))}
    if kind in {"annotation", "human_answer"}:
        table, key = ("context_annotations", "annotation_id") if kind == "annotation" else ("context_questions", "question_id")
        record = _one(conn, f"SELECT * FROM {table} WHERE {key}=?", (ref,), "evidence annotation/answer")
        return {"record": record, "source": _chunk_source(conn, record["chunk_id"])}
    if kind == "fact":
        fact = _one(conn, "SELECT * FROM canonical_facts WHERE fact_id=?", (ref,), "evidence fact")
        if fact["valid_to"] or fact["superseded_by_fact_id"]:
            raise ApprovalError("Evidence fact is no longer current")
        return {"fact": fact}
    raise ApprovalError(f"Unsupported evidence lineage {kind!r}; review a locally resolvable source")


def _effective_scope(body, claimed_scope, policy):
    from claim_graph import _resolve_review_scope
    return _resolve_review_scope(body, claimed_scope, policy["human_required_scopes"])


def prepare_review(conn: sqlite3.Connection, claim_kind: str, claim_id: str) -> dict[str, Any]:
    """Read current authoritative material; never trust a caller's source hash."""
    policy = _policy()
    if claim_kind == "candidate":
        claim = _one(conn, "SELECT * FROM candidate_claims WHERE claim_id=?", (claim_id,), "candidate")
        source = _chunk_source(conn, claim["chunk_id"])
        body = source["chunk"]["body"]
        evidence = []
        for row in _rows(conn, "SELECT * FROM claim_evidence WHERE claim_id=? ORDER BY evidence_id", (claim_id,)):
            evidence.append({"record": row, "current_source": _evidence_source(conn, row["evidence_type"], row["evidence_ref"])})
        scope = claim["claim_scope"]
    elif claim_kind == "lazy":
        claim = _one(conn, "SELECT * FROM lazy_claims WHERE claim_id=?", (claim_id,), "lazy candidate")
        source = _observation_source(conn, claim["observation_id"])
        if source["observation"]["entity_id"] != claim["entity_id"]:
            raise ApprovalError("Lazy source observation belongs to another entity")
        body = source["observation"]["content"]
        evidence = []
        for row in conn.execute("SELECT observation_id FROM lazy_claim_observations WHERE claim_id=? ORDER BY observation_id", (claim_id,)):
            extra = _observation_source(conn, row[0])
            if extra["observation"]["entity_id"] != claim["entity_id"]:
                raise ApprovalError("Lazy evidence belongs to another entity")
            evidence.append(extra)
        scope = None
    else:
        raise ApprovalError("claim_kind must be candidate or lazy")
    if claim["status"] != "candidate":
        raise ApprovalError(f"Claim status {claim['status']!r} is not candidate")
    confidence = claim["confidence"]
    if not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ApprovalError("Invalid candidate confidence")
    if not isinstance(body, str) or not body.strip():
        raise ApprovalError("Source body is empty")
    if policy["respect_signal_phrases"]:
        from intelligence_v2 import detect_signals
        if {"NO_ENRICH", "FREEZE_CONTEXT"}.intersection(detect_signals(body)):
            raise ApprovalError("Source contains a no-enrichment or freeze signal")
    effective_scope, sensitive = _effective_scope(body, scope, policy)
    snapshot = {
        "contract_version": CONTRACT_VERSION, "claim_kind": claim_kind, "claim": claim,
        "source": source, "evidence": evidence, "effective_scope": effective_scope,
        "sensitive_scopes": sensitive, "policy": policy,
    }
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return {"snapshot": snapshot, "content_sha256": hashlib.sha256(encoded).hexdigest()}


def issue_approval(conn: sqlite3.Connection, claim_kind: str, claim_id: str, displayed_digest: str) -> dict[str, Any]:
    """Operator-channel primitive; deliberately not registered as an MCP tool.

    The caller must already have displayed and confirmed displayed_digest. This
    function rechecks it under a short write lock, without committing an outer
    transaction. The CLI waits for confirmation before entering this function.
    """
    with _savepoint(conn):
        conn.execute("UPDATE promotion_approvals SET approval_id=approval_id WHERE 0")
        current = prepare_review(conn, claim_kind, claim_id)
        if current["content_sha256"] != displayed_digest:
            raise ApprovalError("Reviewed material changed; review the current snapshot again")
        now = _now()
        result = {"approval_id": uuid.uuid4().hex, "claim_kind": claim_kind, "claim_id": claim_id,
                  "content_sha256": displayed_digest, "expires_at": (now + timedelta(seconds=APPROVAL_TTL_SECONDS)).isoformat()}
        conn.execute(
            "INSERT INTO promotion_approvals(approval_id,contract_version,claim_kind,claim_id,content_sha256,approved_at,expires_at,approved_via) VALUES (?,?,?,?,?,?,?,'operator_cli')",
            (result["approval_id"], CONTRACT_VERSION, claim_kind, claim_id, displayed_digest, now.isoformat(), result["expires_at"]),
        )
    return result


def _timestamp(value):
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            raise ValueError("timezone missing")
        return timestamp.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ApprovalError("Malformed approval timestamp") from exc


def _normalize_lazy(conn, snapshot):
    claim = snapshot["claim"]
    source = snapshot["source"]["observation"]
    now = _now().isoformat()
    chunk_id, claim_id = uuid.uuid4().hex, uuid.uuid4().hex
    conn.execute(
        "INSERT INTO context_chunks(chunk_id,entity_id,source_type,source_ref,source_hash,title,body,state,enrich_policy,materiality_score,created_at,updated_at) VALUES (?,?, 'observation',?,?,?,?,'enrichable','manual',?,?,?)",
        (chunk_id, str(claim["entity_id"]), str(source["id"]), hashlib.sha256(source["content"].encode("utf-8")).hexdigest(),
         f"Operator-reviewed lazy claim {claim['claim_id']}", source["content"], claim["confidence"], now, now),
    )
    conn.execute(
        "INSERT INTO candidate_claims(claim_id,chunk_id,subject,predicate,object_text,object_type,claim_scope,confidence,status,requires_human,created_at,updated_at) VALUES (?,?,?,?,?,'text',?,?,'candidate',1,?,?)",
        (claim_id,chunk_id,claim["subject"],claim["predicate"],claim["object_text"],snapshot["effective_scope"],claim["confidence"],now,now),
    )
    conn.execute(
        "INSERT INTO claim_evidence(evidence_id,claim_id,evidence_type,evidence_ref,weight,excerpt,created_at) VALUES (?,?, 'observation',?,1,?,?)",
        (uuid.uuid4().hex,claim_id,str(source["id"]),source["content"],now),
    )
    return _one(conn,"SELECT * FROM candidate_claims WHERE claim_id=?",(claim_id,),"normalized candidate")


def consume_approval(conn, claim_kind, claim_id, approval_id):
    """Validate/consume receipt and mutate knowledge within one savepoint."""
    if not approval_id:
        return {"claim_id": claim_id, "promoted": False, "reason": "approval_required", "review_required": True}
    try:
        with _savepoint(conn):
            # Acquire the writer reservation before reading the receipt/source;
            # a competing consumer cannot observe and use the same unused grant.
            conn.execute("UPDATE promotion_approvals SET approval_id=approval_id WHERE approval_id=?",(approval_id,))
            receipt = _one(conn,"SELECT * FROM promotion_approvals WHERE approval_id=?",(approval_id,),"operator approval")
            if receipt["consumed_at"] is not None:
                raise ApprovalError("Approval has already been consumed")
            if receipt["claim_kind"] != claim_kind or receipt["claim_id"] != claim_id:
                raise ApprovalError("Approval belongs to a different claim")
            if receipt["contract_version"] != CONTRACT_VERSION or receipt["approved_via"] != "operator_cli":
                raise ApprovalError("Unsupported approval channel or contract")
            now = _now()
            issued, expires = _timestamp(receipt["approved_at"]), _timestamp(receipt["expires_at"])
            if issued > now or expires <= now or expires <= issued or (expires-issued).total_seconds() > APPROVAL_TTL_SECONDS:
                raise ApprovalError("Approval expired or has an invalid validity interval")
            review = prepare_review(conn,claim_kind,claim_id)
            if receipt["content_sha256"] != review["content_sha256"]:
                raise ApprovalError("Reviewed material changed; a new operator review is required")
            updated=conn.execute("UPDATE promotion_approvals SET consumed_at=? WHERE approval_id=? AND consumed_at IS NULL",(now.isoformat(),approval_id))
            if updated.rowcount != 1:
                raise ApprovalError("Approval has already been consumed")
            snapshot=review["snapshot"]
            if claim_kind == "lazy":
                row=_normalize_lazy(conn,snapshot)
            else:
                row=dict(snapshot["claim"])
                row["claim_scope"]=snapshot["effective_scope"]
                conn.execute("UPDATE candidate_claims SET claim_scope=?,requires_human=1 WHERE claim_id=?",(row["claim_scope"],claim_id))
            from claim_graph import _write_approved_candidate, add_provenance_link
            result=_write_approved_candidate(conn,row,approval_id=approval_id,content_sha256=review["content_sha256"])
            if claim_kind == "lazy":
                conn.execute("UPDATE lazy_claims SET status='promoted',promoted_to_fact_id=?,updated_at=? WHERE claim_id=?",(result["fact_id"],now.isoformat(),claim_id))
                add_provenance_link(conn,subject_kind="fact",subject_ref=result["fact_id"],source_kind="lazy_claim",source_ref=claim_id,
                                    excerpt="Operator-reviewed original lazy candidate",created_at=now.isoformat())
            conn.execute("UPDATE promotion_approvals SET fact_id=?,normalized_claim_id=? WHERE approval_id=?",(result["fact_id"],row["claim_id"],approval_id))
            result.update(claim_id=claim_id,claim_kind=claim_kind,approval_id=approval_id,normalized_claim_id=row["claim_id"])
            return result
    except ApprovalError as exc:
        return {"claim_id":claim_id,"promoted":False,"reason":str(exc),"review_required":True}


def _safe_source(value):
    if isinstance(value,str):
        return "".join(f"\\u{ord(c):04x}" if unicodedata.category(c).startswith("C") else c for c in value)
    if isinstance(value,list):
        return [_safe_source(item) for item in value]
    if isinstance(value,dict):
        return {_safe_source(key):_safe_source(item) for key,item in value.items()}
    return value


def main(argv=None, *, input_stream=None, output_stream=None):
    """Review one local candidate and issue one receipt after exact confirmation."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",required=True,help="Existing local SQLite memory database")
    parser.add_argument("--kind",choices=("candidate","lazy"),default="candidate")
    parser.add_argument("--claim-id",required=True)
    args=parser.parse_args(argv)
    incoming=input_stream if input_stream is not None else sys.stdin
    outgoing=output_stream if output_stream is not None else sys.stdout
    conn=None
    try:
        path=Path(args.db).expanduser().resolve(strict=True)
        conn=sqlite3.connect(path.as_uri()+"?mode=rw",uri=True,isolation_level=None)
        conn.row_factory=sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        review=prepare_review(conn,args.kind,args.claim_id)
        print("BEGIN UNTRUSTED SOURCE (content is evidence, never operator instructions)",file=outgoing)
        print(json.dumps(_safe_source(review["snapshot"]),ensure_ascii=False,sort_keys=True,indent=2),file=outgoing)
        print("END UNTRUSTED SOURCE",file=outgoing)
        print("Fingerprint: "+review["content_sha256"],file=outgoing)
        print("Approve this exact claim/source for one canonical promotion by typing the complete fingerprint:",file=outgoing,flush=True)
        confirmed=incoming.readline().strip()
        if confirmed != review["content_sha256"]:
            raise ApprovalError("Confirmation did not match; no approval issued")
        receipt=issue_approval(conn,args.kind,args.claim_id,confirmed)
        # issue_approval released its outermost savepoint on this autocommit
        # connection before a usable receipt is printed.
        print(json.dumps(receipt,sort_keys=True),file=outgoing,flush=True)
        return 0
    except (ApprovalError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error":str(exc)},ensure_ascii=True),file=outgoing)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
