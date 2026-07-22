"""Memory governance, replay, and self-repair helpers.

Provides:
- append-only event replay over memory_events
- fact validity / contradiction governance primitives
- periodic audit that persists open/resolved issues in memory_audit_issues
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from db_utils import (
    MERGEABLE_FIELDS,
    _event_sort_key,
    _store_task_field_version,
    _normalize_task_status_value,
    _sqlite_has_column,
    _sqlite_table_exists,
    add_knowledge_link,
    add_provenance_link,
    json_dumps,
    json_loads,
    normalize_project_name,
    now_iso,
    record_memory_event,
    upsert_memory_artifact,
)

logger = logging.getLogger("sqlite-kb")

_AUDIT_VERSION = "memory_audit_v2"
_RETRIEVAL_CONTRACT_VERSION = "memory_contract_v2"
_FACT_ACTIONS = ("supersede", "contradict", "invalidate", "revalidate")
_SOURCE_TIMESTAMP_TABLES = {
    "fact": ("canonical_facts", "fact_id", "updated_at", "created_at"),
    "claim": ("candidate_claims", "claim_id", "updated_at", "created_at"),
    "chunk": ("context_chunks", "chunk_id", "updated_at", "created_at"),
    "question": ("context_questions", "question_id", "answered_at", "created_at"),
    "context_pack": ("context_packs", "pack_id", "updated_at", "created_at"),
}
_SQLITE_IN_CHUNK_SIZE = 500


def _new_id() -> str:
    return uuid.uuid4().hex


def _issue_key(
    issue_type: str, subject_kind: str, subject_ref: str
) -> tuple[str, str, str]:
    return issue_type, subject_kind, subject_ref


def _parse_ts(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _batch_source_timestamps(
    conn: sqlite3.Connection, provenance_rows: list[dict[str, str]]
) -> dict[tuple[str, str], str]:
    """Load provenance source timestamps with one bounded query per source kind."""
    refs_by_kind: dict[str, set[str]] = defaultdict(set)
    for row in provenance_rows:
        source_kind = str(row.get("source_kind") or "")
        source_ref = str(row.get("source_ref") or "")
        if source_kind in _SOURCE_TIMESTAMP_TABLES and source_ref:
            refs_by_kind[source_kind].add(source_ref)

    timestamps: dict[tuple[str, str], str] = {}
    for source_kind, refs in refs_by_kind.items():
        table_name, id_column, updated_col, fallback_col = _SOURCE_TIMESTAMP_TABLES[
            source_kind
        ]
        if not _sqlite_table_exists(conn, table_name):
            continue
        table_columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info('{table_name}')")
        }
        if id_column not in table_columns:
            continue
        timestamp_columns = [
            col for col in (updated_col, fallback_col) if col in table_columns
        ]
        if not timestamp_columns:
            continue

        sorted_refs = sorted(refs)
        for offset in range(0, len(sorted_refs), _SQLITE_IN_CHUNK_SIZE):
            ref_batch = sorted_refs[offset : offset + _SQLITE_IN_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in ref_batch)
            rows = conn.execute(
                f"SELECT {id_column}, {', '.join(timestamp_columns)} "
                f"FROM {table_name} WHERE {id_column} IN ({placeholders})",
                ref_batch,
            ).fetchall()
            for row in rows:
                source_ref = str(row[id_column])
                for column in timestamp_columns:
                    if row[column]:
                        timestamps[(source_kind, source_ref)] = str(row[column])
                        break
    return timestamps


def _parse_event_value(raw: Any) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    try:
        return json_loads(raw)
    except Exception:
        return raw


def _materialize_task_field_value(field: str, value: Any) -> str | None:
    """Coerce replayed task field values back into DB-safe task row values."""
    if value is None:
        return None
    if field == "status":
        return _normalize_task_status_value(value)
    if field == "project":
        return normalize_project_name(value)
    if field == "recurring":
        if isinstance(value, str):
            return value
        return json_dumps(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json_dumps(value)
    return str(value)


def _load_task_event_heads(
    conn: sqlite3.Connection,
    task_id: str,
) -> dict[str, dict[str, Any]]:
    if not _sqlite_table_exists(conn, "memory_events"):
        return {}
    rows = conn.execute(
        "SELECT event_id, field_name, new_value, event_ts, machine_id, logical_clock "
        "FROM memory_events WHERE aggregate_kind = 'task' AND aggregate_id = ? "
        "AND field_name IS NOT NULL",
        (task_id,),
    ).fetchall()
    heads: dict[str, dict[str, Any]] = {}
    for row in rows:
        field_name = row["field_name"]
        if not field_name:
            continue
        current = heads.get(field_name)
        candidate = dict(row)
        if current is None or _event_sort_key(
            candidate.get("event_ts"),
            candidate.get("machine_id"),
            int(candidate.get("logical_clock") or 0),
        ) > _event_sort_key(
            current.get("event_ts"),
            current.get("machine_id"),
            int(current.get("logical_clock") or 0),
        ):
            heads[field_name] = candidate
    return heads


def rebuild_task_from_events(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    repair: bool = False,
) -> dict[str, Any]:
    """Rebuild task field state from the event ledger and optionally repair row drift."""
    if not (
        _sqlite_table_exists(conn, "tasks")
        and _sqlite_table_exists(conn, "memory_events")
        and _sqlite_table_exists(conn, "task_field_versions")
    ):
        return {"task_id": task_id, "status": "disabled"}
    row = conn.execute(
        "SELECT "
        + ", ".join([*MERGEABLE_FIELDS, "updated_at"])
        + " FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return {"task_id": task_id, "status": "missing"}

    event_heads = _load_task_event_heads(conn, task_id)
    version_rows = conn.execute(
        "SELECT field_name, updated_at, updated_by, new_value, updated_order, source_event_id "
        "FROM task_field_versions WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    versions = {ver["field_name"]: ver for ver in version_rows}

    rebuilt: dict[str, Any] = {}
    drift: dict[str, dict[str, Any]] = {}
    max_event_ts = ""
    for field in MERGEABLE_FIELDS:
        head = event_heads.get(field)
        if head is not None:
            value = _parse_event_value(head.get("new_value"))
            rebuilt[field] = _materialize_task_field_value(field, value)
            max_event_ts = max(max_event_ts, str(head.get("event_ts") or ""))
        elif field in versions and versions[field]["new_value"] is not None:
            rebuilt[field] = _materialize_task_field_value(
                field,
                _parse_event_value(versions[field]["new_value"]),
            )
            max_event_ts = max(max_event_ts, str(versions[field]["updated_at"] or ""))
        if field in rebuilt and row[field] != rebuilt[field]:
            drift[field] = {
                "materialized": row[field],
                "replayed": rebuilt[field],
            }

    repaired_fields: list[str] = []
    row_updated = str(row["updated_at"] or "")
    if repair and drift and max_event_ts:
        if row_updated > max_event_ts:
            logger.warning(
                "Task %s: updated_at (%s) is ahead of max event ts (%s) — "
                "likely manual UPDATE bypass. Repairing anyway (EB-01 fix).",
                task_id,
                row_updated,
                max_event_ts,
            )
    if repair and drift and max_event_ts:
        set_clause = ", ".join(f"{field} = ?" for field in drift)
        values = [rebuilt[field] for field in drift] + [max_event_ts, task_id]
        conn.execute(
            f"UPDATE tasks SET {set_clause}, updated_at = ? WHERE id = ?",
            values,
        )
        for field in drift:
            version_row = versions.get(field)
            if version_row is not None:
                _store_task_field_version(
                    conn,
                    task_id,
                    field,
                    updated_at=str(version_row["updated_at"] or max_event_ts),
                    updated_by=str(version_row["updated_by"] or ""),
                    new_value=str(version_row["new_value"])
                    if version_row["new_value"] is not None
                    else None,
                    updated_order=int(version_row["updated_order"] or 0),
                    source_event_id=version_row["source_event_id"],
                )
        repaired_fields = sorted(drift)
        # EB-03 fix: record repair in event ledger so future audits see it
        for field in drift:
            record_memory_event(
                conn,
                event_type="repair",
                aggregate_kind="task",
                aggregate_id=task_id,
                field_name=field,
                new_value=str(rebuilt[field]) if rebuilt[field] is not None else None,
            )

    return {
        "task_id": task_id,
        "status": "ok",
        "rebuilt": rebuilt,
        "drift": drift,
        "repaired_fields": repaired_fields,
        "max_event_ts": max_event_ts or None,
    }


def _repair_context_pack_artifacts(conn: sqlite3.Connection) -> int:
    if not (
        _sqlite_table_exists(conn, "context_packs")
        and _sqlite_table_exists(conn, "memory_artifacts")
    ):
        return 0
    repaired = 0
    rows = conn.execute(
        "SELECT pack_id, pack_type, target_ref, body, freshness_score, created_at "
        "FROM context_packs"
    ).fetchall()
    for row in rows:
        provenance = []
        if _sqlite_table_exists(conn, "provenance_links"):
            prov_rows = conn.execute(
                "SELECT source_kind, source_ref, span_start, span_end, excerpt, confidence "
                "FROM provenance_links WHERE subject_kind = 'context_pack' AND subject_ref = ?",
                (row["pack_id"],),
            ).fetchall()
            provenance = [dict(prov) for prov in prov_rows]
        result = upsert_memory_artifact(
            conn,
            artifact_kind="summary",
            scope_kind="context_pack",
            scope_ref=row["pack_id"],
            artifact_key=f"summary:context_pack:{row['pack_id']}",
            title=f"{row['pack_type']} context summary",
            body=row["body"],
            confidence=float(row["freshness_score"] or 0.0),
            created_at=row["created_at"],
            updated_at=row["created_at"],
            provenance=provenance,
            tool_name="memory_audit.repair_context_pack_artifacts",
        )
        if result.get("changed"):
            repaired += 1
    return repaired


def _refresh_contradiction_counts(
    conn: sqlite3.Connection, fact_ids: set[str] | None = None
) -> int:
    """Repair canonical_facts.contradiction_count from active knowledge links."""
    if not (
        _sqlite_table_exists(conn, "canonical_facts")
        and _sqlite_table_exists(conn, "knowledge_links")
        and _sqlite_has_column(conn, "canonical_facts", "contradiction_count")
    ):
        return 0

    if fact_ids:
        placeholders = ", ".join("?" for _ in fact_ids)
        rows = conn.execute(
            "SELECT fact_id FROM canonical_facts WHERE fact_id IN ("
            + placeholders
            + ")",
            tuple(sorted(fact_ids)),
        ).fetchall()
    else:
        rows = conn.execute("SELECT fact_id FROM canonical_facts").fetchall()

    changed = 0
    for row in rows:
        fact_id = row["fact_id"]
        actual = conn.execute(
            "SELECT COUNT(*) AS cnt FROM knowledge_links kl "
            "JOIN canonical_facts cf ON cf.fact_id = kl.object_ref "
            "WHERE kl.subject_kind = 'fact' AND kl.subject_ref = ? "
            "AND kl.relation_type = 'contradicts' AND kl.object_kind = 'fact' "
            "AND kl.active = 1 AND COALESCE(cf.valid_to, '') = ''",
            (fact_id,),
        ).fetchone()["cnt"]
        current = conn.execute(
            "SELECT contradiction_count FROM canonical_facts WHERE fact_id = ?",
            (fact_id,),
        ).fetchone()
        current_value = int(current["contradiction_count"] or 0) if current else 0
        if current_value != actual:
            conn.execute(
                "UPDATE canonical_facts SET contradiction_count = ?, updated_at = ? "
                "WHERE fact_id = ?",
                (actual, now_iso(), fact_id),
            )
            changed += 1
    return changed


def _repair_fact_provenance(conn: sqlite3.Connection) -> int:
    """Backfill fact provenance from source_claim_id and its evidence when possible."""
    if not (
        _sqlite_table_exists(conn, "canonical_facts")
        and _sqlite_table_exists(conn, "provenance_links")
    ):
        return 0
    repaired = 0
    rows = conn.execute(
        "SELECT fact_id, source_claim_id FROM canonical_facts "
        "WHERE COALESCE(valid_to, '') = '' AND source_claim_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        fact_id = row["fact_id"]
        has_provenance = (
            conn.execute(
                "SELECT 1 FROM provenance_links WHERE subject_kind = 'fact' "
                "AND subject_ref = ? LIMIT 1",
                (fact_id,),
            ).fetchone()
            is not None
        )
        if has_provenance:
            continue
        claim_id = row["source_claim_id"]
        add_provenance_link(
            conn,
            subject_kind="fact",
            subject_ref=fact_id,
            source_kind="claim",
            source_ref=claim_id,
            excerpt=f"Backfilled provenance from source_claim_id {claim_id}",
            created_at=now_iso(),
        )
        if _sqlite_table_exists(conn, "claim_evidence"):
            evidence_rows = conn.execute(
                "SELECT evidence_type, evidence_ref, excerpt, source_start, source_end "
                "FROM claim_evidence WHERE claim_id = ?",
                (claim_id,),
            ).fetchall()
            for ev in evidence_rows:
                add_provenance_link(
                    conn,
                    subject_kind="fact",
                    subject_ref=fact_id,
                    source_kind=ev["evidence_type"],
                    source_ref=ev["evidence_ref"],
                    span_start=ev["source_start"]
                    if "source_start" in ev.keys()
                    else None,
                    span_end=ev["source_end"] if "source_end" in ev.keys() else None,
                    excerpt=ev["excerpt"],
                    created_at=now_iso(),
                )
        repaired += 1
    return repaired


def _repair_supersede_links(conn: sqlite3.Connection) -> int:
    if not (
        _sqlite_table_exists(conn, "canonical_facts")
        and _sqlite_table_exists(conn, "knowledge_links")
    ):
        return 0
    repaired = 0
    rows = conn.execute(
        "SELECT fact_id, superseded_by_fact_id FROM canonical_facts "
        "WHERE superseded_by_fact_id IS NOT NULL AND superseded_by_fact_id != ''"
    ).fetchall()
    for row in rows:
        old_fact = row["fact_id"]
        new_fact = row["superseded_by_fact_id"]
        before = conn.total_changes
        add_knowledge_link(
            conn,
            subject_kind="fact",
            subject_ref=new_fact,
            relation_type="supersedes",
            object_kind="fact",
            object_ref=old_fact,
            rationale=f"Repair from canonical_facts.superseded_by_fact_id on {old_fact}",
            created_at=now_iso(),
        )
        add_knowledge_link(
            conn,
            subject_kind="fact",
            subject_ref=old_fact,
            relation_type="superseded_by",
            object_kind="fact",
            object_ref=new_fact,
            rationale=f"Repair from canonical_facts.superseded_by_fact_id on {old_fact}",
            created_at=now_iso(),
        )
        if conn.total_changes > before:
            repaired += 1
    return repaired


def govern_fact(
    conn: sqlite3.Connection,
    fact_id: str,
    action: str,
    *,
    target_fact_id: str | None = None,
    rationale: str | None = None,
    effective_at: str | None = None,
) -> dict[str, Any]:
    """Apply truth-maintenance action to a canonical fact."""
    if action not in _FACT_ACTIONS:
        return {"error": f"Unsupported action: {action}. Use one of {_FACT_ACTIONS}"}
    if not _sqlite_table_exists(conn, "canonical_facts"):
        return {"error": "canonical_facts table not available"}

    row = conn.execute(
        "SELECT fact_id, subject, predicate, object_text, valid_to, superseded_by_fact_id "
        "FROM canonical_facts WHERE fact_id = ?",
        (fact_id,),
    ).fetchone()
    if row is None:
        return {"error": f"Fact '{fact_id}' not found"}

    now = now_iso()
    valid_at = effective_at or now
    changed = False

    if action == "supersede":
        if not target_fact_id:
            return {"error": "target_fact_id is required for supersede"}
        if target_fact_id == fact_id:
            return {"error": "A fact cannot supersede itself"}
        target_row = conn.execute(
            "SELECT fact_id FROM canonical_facts WHERE fact_id = ?",
            (target_fact_id,),
        ).fetchone()
        if target_row is None:
            return {"error": f"Target fact '{target_fact_id}' not found"}
        conn.execute(
            "UPDATE canonical_facts SET valid_to = ?, superseded_by_fact_id = ?, "
            "updated_at = ? WHERE fact_id = ?",
            (valid_at, target_fact_id, now, fact_id),
        )
        add_knowledge_link(
            conn,
            subject_kind="fact",
            subject_ref=target_fact_id,
            relation_type="supersedes",
            object_kind="fact",
            object_ref=fact_id,
            rationale=rationale or f"{target_fact_id} supersedes {fact_id}",
            created_at=now,
        )
        add_knowledge_link(
            conn,
            subject_kind="fact",
            subject_ref=fact_id,
            relation_type="superseded_by",
            object_kind="fact",
            object_ref=target_fact_id,
            rationale=rationale or f"{fact_id} superseded by {target_fact_id}",
            created_at=now,
        )
        changed = True
    elif action == "contradict":
        if not target_fact_id:
            return {"error": "target_fact_id is required for contradict"}
        if target_fact_id == fact_id:
            return {"error": "A fact cannot contradict itself"}
        target_row = conn.execute(
            "SELECT fact_id FROM canonical_facts WHERE fact_id = ?",
            (target_fact_id,),
        ).fetchone()
        if target_row is None:
            return {"error": f"Target fact '{target_fact_id}' not found"}
        add_knowledge_link(
            conn,
            subject_kind="fact",
            subject_ref=fact_id,
            relation_type="contradicts",
            object_kind="fact",
            object_ref=target_fact_id,
            rationale=rationale or f"{fact_id} contradicts {target_fact_id}",
            created_at=now,
        )
        add_knowledge_link(
            conn,
            subject_kind="fact",
            subject_ref=target_fact_id,
            relation_type="contradicts",
            object_kind="fact",
            object_ref=fact_id,
            rationale=rationale or f"{target_fact_id} contradicts {fact_id}",
            created_at=now,
        )
        changed = True
    elif action == "invalidate":
        conn.execute(
            "UPDATE canonical_facts SET valid_to = ?, updated_at = ? WHERE fact_id = ?",
            (valid_at, now, fact_id),
        )
        changed = True
    elif action == "revalidate":
        conn.execute(
            "UPDATE canonical_facts SET valid_to = NULL, superseded_by_fact_id = NULL, "
            "updated_at = ? WHERE fact_id = ?",
            (now, fact_id),
        )
        if _sqlite_table_exists(conn, "knowledge_links"):
            conn.execute(
                "UPDATE knowledge_links SET active = 0 WHERE active = 1 "
                "AND subject_kind = 'fact' AND relation_type IN ('supersedes', 'superseded_by') "
                "AND (subject_ref = ? OR object_ref = ?)",
                (fact_id, fact_id),
            )
        changed = True

    if not changed:
        return {"changed": False}

    if action == "supersede":
        _repair_supersede_links(conn)
    _refresh_contradiction_counts(
        conn, {fact_id} | ({target_fact_id} if target_fact_id else set())
    )
    event_meta = record_memory_event(
        conn,
        event_type=f"fact_{action}",
        aggregate_kind="fact",
        aggregate_id=fact_id,
        tool_name="sqlite-intel.govern_fact",
        event_ts=now,
        old_value={
            "valid_to": row["valid_to"],
            "superseded_by_fact_id": row["superseded_by_fact_id"],
        },
        new_value={
            "action": action,
            "target_fact_id": target_fact_id,
            "effective_at": valid_at,
        },
        payload={
            "fact_id": fact_id,
            "action": action,
            "target_fact_id": target_fact_id,
            "rationale": rationale,
            "audit_version": _AUDIT_VERSION,
        },
        source_kind="fact",
        source_ref=target_fact_id or fact_id,
        source_excerpt=rationale,
    )
    provenance = [
        {
            "source_kind": "fact",
            "source_ref": fact_id,
            "excerpt": rationale or f"{action} {fact_id}",
        }
    ]
    if target_fact_id:
        provenance.append(
            {
                "source_kind": "fact",
                "source_ref": target_fact_id,
                "excerpt": rationale or f"{action} {target_fact_id}",
            }
        )
    upsert_memory_artifact(
        conn,
        artifact_kind="decision",
        scope_kind="fact",
        scope_ref=fact_id,
        artifact_key=f"decision:fact:{fact_id}:{action}:{target_fact_id or ''}:{valid_at}",
        title=f"Fact {action}",
        body=(
            f"Action: {action}\n"
            f"Fact: {fact_id}\n"
            f"Target: {target_fact_id or '-'}\n"
            f"Effective at: {valid_at}\n"
            f"Rationale: {rationale or '(none)'}"
        ),
        confidence=1.0,
        valid_from=valid_at,
        source_event_id=event_meta.get("event_id"),
        updated_at=now,
        provenance=provenance,
        tool_name="sqlite-intel.govern_fact",
    )
    return {
        "fact_id": fact_id,
        "action": action,
        "target_fact_id": target_fact_id,
        "effective_at": valid_at,
        "changed": True,
    }


def replay_memory_events(
    conn: sqlite3.Connection,
    *,
    aggregate_kind: str | None = None,
    aggregate_id: str | None = None,
    limit: int = 100,
    since_ts: str | None = None,
) -> dict[str, Any]:
    """Return deterministic replay slice from the append-only memory ledger."""
    if not _sqlite_table_exists(conn, "memory_events"):
        return {"events": [], "count": 0}

    conditions: list[str] = []
    params: list[Any] = []
    if aggregate_kind:
        conditions.append("aggregate_kind = ?")
        params.append(aggregate_kind)
    if aggregate_id:
        conditions.append("aggregate_id = ?")
        params.append(aggregate_id)
    if since_ts:
        conditions.append("event_ts >= ?")
        params.append(since_ts)

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        "SELECT event_id, event_type, aggregate_kind, aggregate_id, field_name, "
        "actor_type, actor_id, machine_id, tool_name, logical_clock, event_ts, "
        "old_value, new_value, payload_json, parent_event_id, source_kind, source_ref, "
        "source_excerpt, source_start, source_end "
        f"FROM memory_events {where_sql} "
        "ORDER BY event_ts DESC, machine_id DESC, logical_clock DESC LIMIT ?",
        (*params, max(1, min(limit, 500))),
    ).fetchall()

    events: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("old_value", "new_value", "payload_json"):
            raw = item.get(key)
            if not raw:
                continue
            try:
                item[key] = json_loads(raw)
            except Exception:
                pass
        events.append(item)

    return {
        "count": len(events),
        "events": events,
        "contract_version": _AUDIT_VERSION,
    }


def _upsert_audit_issue(
    conn: sqlite3.Connection,
    *,
    issue_type: str,
    severity: str,
    subject_kind: str,
    subject_ref: str,
    details: dict[str, Any],
    detected_at: str,
) -> str:
    existing = conn.execute(
        "SELECT issue_id, first_detected_at FROM memory_audit_issues "
        "WHERE issue_type = ? AND subject_kind = ? AND subject_ref = ? "
        "ORDER BY first_detected_at ASC LIMIT 1",
        (issue_type, subject_kind, subject_ref),
    ).fetchone()
    details_json = json_dumps(details)
    if existing:
        conn.execute(
            "UPDATE memory_audit_issues SET severity = ?, details_json = ?, "
            "status = 'open', last_detected_at = ?, resolved_at = NULL WHERE issue_id = ?",
            (severity, details_json, detected_at, existing["issue_id"]),
        )
        return existing["issue_id"]

    issue_id = _new_id()
    conn.execute(
        "INSERT INTO memory_audit_issues "
        "(issue_id, issue_type, severity, subject_kind, subject_ref, details_json, "
        "status, first_detected_at, last_detected_at, resolved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, NULL)",
        (
            issue_id,
            issue_type,
            severity,
            subject_kind,
            subject_ref,
            details_json,
            detected_at,
            detected_at,
        ),
    )
    return issue_id


def list_memory_audit_issues(
    conn: sqlite3.Connection, *, status: str = "open", limit: int = 100
) -> dict[str, Any]:
    if not _sqlite_table_exists(conn, "memory_audit_issues"):
        return {"issues": [], "count": 0}
    rows = conn.execute(
        "SELECT issue_id, issue_type, severity, subject_kind, subject_ref, details_json, "
        "status, first_detected_at, last_detected_at, resolved_at "
        "FROM memory_audit_issues WHERE status = ? "
        "ORDER BY last_detected_at DESC, severity DESC LIMIT ?",
        (status, max(1, min(limit, 500))),
    ).fetchall()
    issues = []
    for row in rows:
        item = dict(row)
        raw = item.get("details_json")
        if raw:
            try:
                item["details_json"] = json_loads(raw)
            except Exception:
                pass
        issues.append(item)
    return {"count": len(issues), "issues": issues, "audit_version": _AUDIT_VERSION}


def run_memory_audit(
    conn: sqlite3.Connection,
    *,
    repair: bool = True,
    stale_sync_minutes: int = 120,
    emit_event: bool = True,
) -> dict[str, Any]:
    """Run memory health audit and persist open/resolved issues."""
    if not _sqlite_table_exists(conn, "memory_audit_issues"):
        return {"status": "disabled", "audit_version": _AUDIT_VERSION}

    detected_at = now_iso()
    open_keys: set[tuple[str, str, str]] = set()
    issues: list[dict[str, Any]] = []
    repairs = {
        "fact_provenance_backfilled": 0,
        "supersede_links_repaired": 0,
        "contradiction_counts_refreshed": 0,
        "context_pack_summaries_materialized": 0,
        "task_materialization_reconciled": 0,
    }

    if repair:
        repairs["fact_provenance_backfilled"] = _repair_fact_provenance(conn)
        repairs["supersede_links_repaired"] = _repair_supersede_links(conn)
        repairs["contradiction_counts_refreshed"] = _refresh_contradiction_counts(conn)
        repairs["context_pack_summaries_materialized"] = _repair_context_pack_artifacts(
            conn
        )

    provenance_by_subject: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(
        list
    )
    timestamp_provenance: list[dict[str, str]] = []
    if _sqlite_table_exists(conn, "provenance_links"):
        provenance_rows = conn.execute(
            "SELECT subject_kind, subject_ref, source_kind, source_ref "
            "FROM provenance_links "
            "WHERE subject_kind IN ('fact', 'context_pack', 'artifact')"
        ).fetchall()
        for row in provenance_rows:
            item = {
                "source_kind": str(row["source_kind"]),
                "source_ref": str(row["source_ref"]),
            }
            subject_key = (str(row["subject_kind"]), str(row["subject_ref"]))
            provenance_by_subject[subject_key].append(item)
            if row["subject_kind"] in {"context_pack", "artifact"}:
                timestamp_provenance.append(item)
    source_timestamps = _batch_source_timestamps(conn, timestamp_provenance)

    candidate_claim_ids: set[str] = set()
    candidate_claims_exists = _sqlite_table_exists(conn, "candidate_claims")
    if candidate_claims_exists:
        candidate_claim_ids = {
            str(row[0]) for row in conn.execute("SELECT claim_id FROM candidate_claims")
        }

    def add_issue(
        issue_type: str,
        severity: str,
        subject_kind: str,
        subject_ref: str,
        details: dict[str, Any],
    ) -> None:
        open_keys.add(_issue_key(issue_type, subject_kind, subject_ref))
        issue_id = _upsert_audit_issue(
            conn,
            issue_type=issue_type,
            severity=severity,
            subject_kind=subject_kind,
            subject_ref=subject_ref,
            details=details,
            detected_at=detected_at,
        )
        issues.append(
            {
                "issue_id": issue_id,
                "issue_type": issue_type,
                "severity": severity,
                "subject_kind": subject_kind,
                "subject_ref": subject_ref,
                "details": details,
            }
        )

    if _sqlite_table_exists(conn, "candidate_claims") and _sqlite_table_exists(
        conn, "claim_evidence"
    ):
        rows = conn.execute(
            "SELECT c.claim_id, c.subject, c.predicate, c.object_text FROM candidate_claims c "
            "LEFT JOIN claim_evidence ce ON ce.claim_id = c.claim_id "
            "WHERE ce.claim_id IS NULL"
        ).fetchall()
        for row in rows:
            add_issue(
                "claim_missing_evidence",
                "high",
                "claim",
                row["claim_id"],
                {
                    "subject": row["subject"],
                    "predicate": row["predicate"],
                    "object_text": row["object_text"],
                },
            )

        rows = conn.execute(
            "SELECT claim_id, promoted_to_fact_id FROM candidate_claims "
            "WHERE status = 'promoted' AND promoted_to_fact_id IS NOT NULL "
            "AND promoted_to_fact_id NOT IN (SELECT fact_id FROM canonical_facts)"
        ).fetchall()
        for row in rows:
            add_issue(
                "promoted_claim_missing_fact",
                "high",
                "claim",
                row["claim_id"],
                {"promoted_to_fact_id": row["promoted_to_fact_id"]},
            )

    if _sqlite_table_exists(conn, "canonical_facts"):
        rows = conn.execute(
            "SELECT fact_id, subject, predicate, object_text, source_claim_id "
            "FROM canonical_facts WHERE COALESCE(valid_to, '') = ''"
        ).fetchall()
        for row in rows:
            fact_id = row["fact_id"]
            has_provenance = bool(provenance_by_subject.get(("fact", str(fact_id))))
            if not has_provenance:
                add_issue(
                    "fact_missing_provenance",
                    "high",
                    "fact",
                    fact_id,
                    {
                        "subject": row["subject"],
                        "predicate": row["predicate"],
                        "object_text": row["object_text"],
                        "source_claim_id": row["source_claim_id"],
                    },
                )

            if row["source_claim_id"] and candidate_claims_exists:
                if str(row["source_claim_id"]) not in candidate_claim_ids:
                    add_issue(
                        "fact_missing_source_claim",
                        "medium",
                        "fact",
                        fact_id,
                        {"source_claim_id": row["source_claim_id"]},
                    )

    if _sqlite_table_exists(conn, "knowledge_links") and _sqlite_table_exists(
        conn, "canonical_facts"
    ):
        rows = conn.execute(
            "SELECT kl.subject_ref AS fact_a, kl.object_ref AS fact_b "
            "FROM knowledge_links kl "
            "JOIN canonical_facts fa ON fa.fact_id = kl.subject_ref "
            "JOIN canonical_facts fb ON fb.fact_id = kl.object_ref "
            "WHERE kl.subject_kind = 'fact' AND kl.object_kind = 'fact' "
            "AND kl.relation_type = 'contradicts' AND kl.active = 1 "
            "AND COALESCE(fa.valid_to, '') = '' AND COALESCE(fb.valid_to, '') = ''"
        ).fetchall()
        seen_pairs: set[str] = set()
        for row in rows:
            pair = "|".join(sorted((row["fact_a"], row["fact_b"])))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            add_issue(
                "unresolved_contradiction",
                "high",
                "fact_pair",
                pair,
                {"fact_ids": pair.split("|")},
            )

    if _sqlite_table_exists(conn, "context_packs"):
        rows = conn.execute(
            "SELECT pack_id, pack_type, target_ref, created_at, contract_version "
            "FROM context_packs"
        ).fetchall()
        for row in rows:
            pack_id = row["pack_id"]
            prov_rows = provenance_by_subject.get(("context_pack", str(pack_id)), [])
            if not prov_rows:
                add_issue(
                    "context_pack_missing_provenance",
                    "medium",
                    "context_pack",
                    pack_id,
                    {
                        "pack_type": row["pack_type"],
                        "target_ref": row["target_ref"],
                    },
                )
            stale_sources: list[dict[str, str]] = []
            created_at = _parse_ts(row["created_at"])
            if created_at:
                for prov in prov_rows:
                    source_kind = prov["source_kind"]
                    source_ref = prov["source_ref"]
                    updated_at = source_timestamps.get((source_kind, source_ref))
                    updated_dt = _parse_ts(updated_at)
                    if updated_dt and updated_dt > created_at:
                        stale_sources.append(
                            {
                                "source_kind": source_kind,
                                "source_ref": source_ref,
                                "updated_at": updated_at or "",
                            }
                        )
                if stale_sources:
                    add_issue(
                        "context_pack_stale",
                        "medium",
                        "context_pack",
                        pack_id,
                        {
                            "pack_type": row["pack_type"],
                            "stale_sources": stale_sources[:10],
                        },
                    )
            contract_version = (
                row["contract_version"]
                if "contract_version" in row.keys()
                else "legacy"
            )
            if contract_version != _RETRIEVAL_CONTRACT_VERSION:
                add_issue(
                    "context_pack_contract_legacy",
                    "low",
                    "context_pack",
                    pack_id,
                    {"contract_version": contract_version},
                )

    if _sqlite_table_exists(conn, "memory_artifacts"):
        rows = conn.execute(
            "SELECT artifact_id, artifact_kind, scope_kind, scope_ref, title, updated_at "
            "FROM memory_artifacts WHERE COALESCE(valid_to, '') = ''"
        ).fetchall()
        for row in rows:
            artifact_id = row["artifact_id"]
            prov_rows = provenance_by_subject.get(("artifact", str(artifact_id)), [])
            if not prov_rows:
                add_issue(
                    "artifact_missing_provenance",
                    "high",
                    "artifact",
                    artifact_id,
                    {
                        "artifact_kind": row["artifact_kind"],
                        "scope_kind": row["scope_kind"],
                        "scope_ref": row["scope_ref"],
                        "title": row["title"],
                    },
                )
            stale_sources: list[dict[str, str]] = []
            artifact_dt = _parse_ts(row["updated_at"])
            if artifact_dt:
                for prov in prov_rows:
                    updated_at = source_timestamps.get(
                        (prov["source_kind"], prov["source_ref"])
                    )
                    updated_dt = _parse_ts(updated_at)
                    if updated_dt and updated_dt > artifact_dt:
                        stale_sources.append(
                            {
                                "source_kind": prov["source_kind"],
                                "source_ref": prov["source_ref"],
                                "updated_at": updated_at or "",
                            }
                        )
                if stale_sources:
                    add_issue(
                        "artifact_stale",
                        "medium",
                        "artifact",
                        artifact_id,
                        {
                            "artifact_kind": row["artifact_kind"],
                            "stale_sources": stale_sources[:10],
                        },
                    )

    if _sqlite_table_exists(conn, "tasks") and _sqlite_table_exists(
        conn, "memory_events"
    ):
        rows = conn.execute("SELECT id, updated_at FROM tasks").fetchall()
        for row in rows:
            rebuilt = rebuild_task_from_events(conn, row["id"], repair=repair)
            repaired_fields = rebuilt.get("repaired_fields", [])
            if repaired_fields:
                repairs["task_materialization_reconciled"] += len(repaired_fields)
                continue
            drift = rebuilt.get("drift", {})
            if not drift:
                continue
            max_event_ts = rebuilt.get("max_event_ts")
            issue_type = (
                "task_write_bypass"
                if max_event_ts and str(row["updated_at"] or "") > str(max_event_ts)
                else "task_materialization_drift"
            )
            add_issue(
                issue_type,
                "high" if issue_type == "task_write_bypass" else "medium",
                "task",
                row["id"],
                {
                    "drift_fields": drift,
                    "task_updated_at": row["updated_at"],
                    "max_event_ts": max_event_ts,
                },
            )

    if _sqlite_table_exists(conn, "memory_conflicts"):
        rows = conn.execute(
            "SELECT conflict_id, aggregate_kind, aggregate_id, field_name, winner, rationale "
            "FROM memory_conflicts WHERE status = 'open'"
        ).fetchall()
        for row in rows:
            add_issue(
                "memory_conflict_open",
                "medium",
                row["aggregate_kind"],
                row["conflict_id"],
                {
                    "aggregate_id": row["aggregate_id"],
                    "field_name": row["field_name"],
                    "winner": row["winner"],
                    "rationale": row["rationale"],
                },
            )

    if _sqlite_table_exists(conn, "memory_events") and _sqlite_table_exists(
        conn, "bridge_meta"
    ):
        max_event = conn.execute(
            "SELECT MAX(event_ts) AS max_event_ts FROM memory_events"
        ).fetchone()
        last_push = conn.execute(
            "SELECT value FROM bridge_meta WHERE key = 'last_push_at'"
        ).fetchone()
        max_event_ts = max_event["max_event_ts"] if max_event else None
        last_push_at = last_push["value"] if last_push else None
        max_event_dt = _parse_ts(max_event_ts)
        last_push_dt = _parse_ts(last_push_at)
        if max_event_dt and last_push_dt:
            delta_minutes = (max_event_dt - last_push_dt).total_seconds() / 60.0
            if delta_minutes > stale_sync_minutes:
                add_issue(
                    "sync_drift",
                    "medium",
                    "bridge",
                    "main",
                    {
                        "last_push_at": last_push_at,
                        "latest_event_at": max_event_ts,
                        "delta_minutes": round(delta_minutes, 1),
                    },
                )

    open_rows = conn.execute(
        "SELECT issue_id, issue_type, subject_kind, subject_ref FROM memory_audit_issues "
        "WHERE status = 'open'"
    ).fetchall()
    resolved = 0
    for row in open_rows:
        key = _issue_key(row["issue_type"], row["subject_kind"], row["subject_ref"])
        if key in open_keys:
            continue
        conn.execute(
            "UPDATE memory_audit_issues SET status = 'resolved', resolved_at = ?, "
            "last_detected_at = ? WHERE issue_id = ?",
            (detected_at, detected_at, row["issue_id"]),
        )
        resolved += 1

    if emit_event and (issues or resolved or any(repairs.values())):
        record_memory_event(
            conn,
            event_type="memory_audit_run",
            aggregate_kind="audit",
            aggregate_id="memory",
            tool_name="sqlite-intel.audit_memory",
            event_ts=detected_at,
            new_value={
                "open_issues": len(issues),
                "resolved_issues": resolved,
                "repairs": repairs,
            },
            payload={
                "audit_version": _AUDIT_VERSION,
                "repair": repair,
                "stale_sync_minutes": stale_sync_minutes,
            },
        )

    return {
        "audit_version": _AUDIT_VERSION,
        "repair": repair,
        "emit_event": emit_event,
        "open_issue_count": len(issues),
        "resolved_issue_count": resolved,
        "issues": issues,
        "repairs": repairs,
    }


def maybe_run_memory_audit(
    conn: sqlite3.Connection,
    *,
    runner_name: str = "background",
    cadence_minutes: int = 60,
    repair: bool = True,
    stale_sync_minutes: int = 120,
    force: bool = False,
    emit_event: bool = True,
) -> dict[str, Any]:
    """Run the audit only when due, persisting scheduler state in the DB."""
    if not _sqlite_table_exists(conn, "memory_audit_state"):
        return run_memory_audit(
            conn,
            repair=repair,
            stale_sync_minutes=stale_sync_minutes,
            emit_event=emit_event,
        )

    now = now_iso()
    row = conn.execute(
        "SELECT cadence_minutes, next_run_after, last_status FROM memory_audit_state "
        "WHERE runner_name = ?",
        (runner_name,),
    ).fetchone()
    cadence = max(
        5, int((row["cadence_minutes"] if row else cadence_minutes) or cadence_minutes)
    )
    next_run_after = row["next_run_after"] if row else None
    next_dt = _parse_ts(next_run_after)
    if not force and next_dt and next_dt > _parse_ts(now):
        return {
            "status": "skipped_due",
            "runner_name": runner_name,
            "next_run_after": next_run_after,
            "audit_version": _AUDIT_VERSION,
        }

    conn.execute(
        "INSERT INTO memory_audit_state ("
        "runner_name, cadence_minutes, last_started_at, last_finished_at, next_run_after, "
        "last_status, last_summary_json, updated_at"
        ") VALUES (?, ?, ?, NULL, NULL, 'running', NULL, ?) "
        "ON CONFLICT(runner_name) DO UPDATE SET "
        "cadence_minutes = excluded.cadence_minutes, "
        "last_started_at = excluded.last_started_at, "
        "last_status = 'running', updated_at = excluded.updated_at",
        (runner_name, cadence, now, now),
    )
    try:
        result = run_memory_audit(
            conn,
            repair=repair,
            stale_sync_minutes=stale_sync_minutes,
            emit_event=emit_event,
        )
        finished = now_iso()
        next_after = (_parse_ts(finished) + timedelta(minutes=cadence)).isoformat()
        conn.execute(
            "UPDATE memory_audit_state SET last_finished_at = ?, next_run_after = ?, "
            "last_status = 'success', last_summary_json = ?, updated_at = ? "
            "WHERE runner_name = ?",
            (finished, next_after, json_dumps(result), finished, runner_name),
        )
        result["runner_name"] = runner_name
        result["scheduled_next_run_after"] = next_after
        return result
    except Exception as exc:
        failed = now_iso()
        conn.execute(
            "UPDATE memory_audit_state SET last_finished_at = ?, next_run_after = ?, "
            "last_status = 'error', last_summary_json = ?, updated_at = ? "
            "WHERE runner_name = ?",
            (
                failed,
                (_parse_ts(failed) + timedelta(minutes=cadence)).isoformat(),
                json_dumps({"error": str(exc)}),
                failed,
                runner_name,
            ),
        )
        raise
