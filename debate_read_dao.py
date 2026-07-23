"""Read-only debate access for the native tray (BUILD STEP 1, read-only stages).

Faithful port of the proven, no-LLM logic in
``operator_board/board.py`` (recent / _section_a / topics / topic_thread /
search), per BOARD-TO-NATIVE-TRAY-SPEC-2026-07-18.md (§2, §4). This module is
**read-only by construction**:

* every connection is opened ``mode=ro`` **and** ``PRAGMA query_only=ON``;
* it exposes **no** mutation / close / CAS entry point, and never shells out;
* an optional ``forbid_path`` fail-closed guard refuses to open a DB whose
  realpath matches a forbidden path (the harness passes the prod DB so tests
  are structurally unable to touch prod — spec §6 B5 lab/prod fence).

Determinism (spec M3): the "now" used by ``recent`` / ``waiting_section_a`` is
supplied by an injected ``clock`` callable. The acceptance harness injects the
frozen ``as_of``; there is **no** live ``datetime.now`` on the harness path.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

# ── Section-A predicate regexes — verbatim from board.py:243-273 ──────────────
_A_REF = re.compile(
    r"(записан|получен|дошъл|даде|даден|дадено|взето|заключен|landed|recorded|"
    r"gave|given|granted|verbatim|давай|\bACK\b|per\s+operator|"
    r"по\s+операторск\w*\s+директив|availability\s+override|поеми|разпоред|"
    r"standing=|DECISION\s+brief|→\s*ADVOCATE)",
    re.I,
)
_A_TAKEN = re.compile(
    r"(операторск\w*\s+(?:GO|решени\w+)\s+(?:записан|получен|даден|взето)|"
    r"оператор\w*\s+(?:даде|потвърди|реши|одобри|нареди|разпореди)|"
    r"operator\s+(?:gave|approved|confirmed|decided)|давай|verbatim\s+GO|ЗАПИСАН)",
    re.I,
)
_A_OP_AWAIT = re.compile(
    r"(чака\w*\s+оператор|очаква\w*\s+оператор|awaiting\s+operator|pending\s+operator|"
    r"operator\s+GO\s+(?:needed|required|pending|awaited)|"
    r"operator\s+(?:decision|sign-?off|input|approval)\s+"
    r"(?:needed|required|awaited|pending|requested)|"
    r"нужен\s+операторск|нужн\w*\s+операторск|изисква\w*\s+операторск|"
    r"за\s+операторско\s+реш|моля\s+оператор|"
    r"needs?\s+operator\s+(?:decision|go|sign|input|approval))",
    re.I,
)
_A_IS_OPERATOR_ROLE = re.compile(r"^\s*(human|operator|оператор)", re.I)

# A later descendant can close an otherwise valid operator-await message
# without being authored by a `human-*` role. Keep this deliberately narrow:
# generic words such as "затварям" or "PASS" may refer to another lane in the
# same long reply and must not consume the operator's still-pending action.
_A_OPERATOR_ACTION_RESOLVED = re.compile(
    r"(^|\n)[^\n]{0,48}операторск\w*\s+решени\w*\b|"
    r"операторск\w*\s+(?:GO|верификац\w*|потвърждени\w*)\s+"
    r"(?:записан\w*|получен\w*|даден\w*|взет\w*|изпълнен\w*)|"
    r"финален\s+инсталиран\s+стек[\s\S]{0,240}всичко\s+работи",
    re.I,
)


# ── clock-free helpers (verbatim board.py:166-224) ───────────────────────────
def parse_ts(ts):
    if not ts:
        return None
    s = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def one_line(body, n=140):
    if not body:
        return ""
    for raw in body.splitlines():
        line = raw.strip()
        if line:
            return line[:n] + ("…" if len(line) > n else "")
    return body.strip()[:n]


def fts_expr(query, mode="and"):
    toks = [t for t in re.findall(r"\w+", query or "", re.UNICODE) if t]
    if not toks:
        return ""
    parts = [f'"{t}"*' for t in toks]
    joiner = " OR " if mode == "or" else " "
    return joiner.join(parts)


class DebateReadDAO:
    """Read-only debate/knowledge reads for the tray's three new tabs + search.

    Parameters
    ----------
    db_path : str
        Path to the SQLite DB. Opened ``mode=ro`` + ``PRAGMA query_only=ON``.
    clock : callable | None
        Returns an aware UTC ``datetime`` for "now". Defaults to live UTC.
        The acceptance harness injects the frozen ``as_of``.
    forbid_path : str | None
        If set and ``realpath(db_path) == realpath(forbid_path)``, construction
        raises ``PermissionError`` (fail-closed prod fence for the harness).
    """

    def __init__(self, db_path, *, clock=None, forbid_path=None):
        self.db_path = db_path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if forbid_path is not None:
            if os.path.realpath(db_path) == os.path.realpath(forbid_path):
                raise PermissionError(
                    f"DebateReadDAO refused forbidden DB path: {db_path!r} "
                    f"resolves to the fenced path {forbid_path!r}"
                )
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA query_only=ON")  # structural write-block
        self._caps = self._introspect()
        self._debate_mem = None  # lazy in-memory FTS mirror for search

    # ---- introspection (board.py:66-92) ------------------------------------
    def _introspect(self):
        names = {
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }

        def cols(t):
            if t not in names:
                return set()
            return {r[1] for r in self._conn.execute(f"PRAGMA table_info({t})")}

        return {
            "tasks": cols("tasks"),
            "task_field_versions": cols("task_field_versions"),
            "has_recipients": "debate_message_recipients" in names,
            "has_tasks_fts": "tasks_fts" in names,
            "has_memory_fts": "memory_fts" in names,
            "has_debate": "debate_messages" in names,
            "has_tasks": "tasks" in names,
            "has_task_field_versions": "task_field_versions" in names,
            "has_debates_tbl": "debates" in names,
            "debate_messages": cols("debate_messages"),
            "has_blind_commits": "debate_blind_commits" in names,
            "has_human_packets": "debate_human_packets" in names,
            "has_protocol_state": "debate_protocol_state" in names,
            "has_link_decisions": "link_suggestion_decisions" in names,
            "has_task_entity_links": "task_entity_links" in names,
            "has_entities": "entities" in names,
        }

    def _visible_sql(self, alias="m"):
        """Human/tray visibility: never expose an unreleased blind CLAIM."""
        if not self._caps.get("has_blind_commits"):
            return "1=1"
        return (
            "NOT EXISTS (SELECT 1 FROM debate_blind_commits bc "
            f"WHERE bc.msg_id={alias}.msg_id AND bc.released_at IS NULL)"
        )

    def _now(self):
        return self._clock()

    def _rel(self, ts):
        dt = parse_ts(ts)
        if dt is None:
            return "—"
        sec = int((self._now() - dt).total_seconds())
        if sec < 0:
            return "сега"
        if sec < 60:
            return f"преди {sec} сек"
        m = sec // 60
        if m < 60:
            return f"преди {m} мин"
        h = m // 60
        if h < 24:
            return f"преди {h} ч"
        d = h // 24
        if d < 30:
            return f"преди {d} дни"
        mo = d // 30
        if mo < 12:
            return f"преди {mo} мес"
        return f"преди {d // 365} год"

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._debate_mem is not None:
            self._debate_mem.close()
            self._debate_mem = None

    # ---- VIEW 1a: waiting section A (board.py:308-427) ----------------------
    def waiting_section_a(self, *, live_await=True):
        """Section A = what is waiting on the operator.

        Layer 1 = verbatim board `_section_a` (kept intact → T2 board parity).
        Layer 2 (`live_await=True`, default) handles ledgers where operator asks
        are routed to team roles rather than a `human-*` recipient. It accepts
        only explicit waiting/needed/pending language from `_A_OP_AWAIT`.
        Historical provenance and guardrails such as "operator GO", "operator
        hand", "ratified", or "say" are not asks by themselves. A later
        descendant that records the operator action (or a final completed
        install) resolves the row. `live_await=False` returns layer 1 only
        (byte-identical to board; used by the T2 parity test).
        """
        if not self._caps["has_debate"]:
            return [], 0
        con = self._conn
        rows = con.execute(
            "SELECT msg_id, role, kind, priority, "
            "COALESCE(ts, created_at) AS ts, reply_to, COALESCE(body,'') AS body "
            "FROM debate_messages m WHERE " + self._visible_sql("m")
        ).fetchall()
        byid = {r["msg_id"]: r for r in rows}
        children = {}
        for r in rows:
            if r["reply_to"]:
                children.setdefault(r["reply_to"], []).append(r["msg_id"])

        def descendants(mid):
            seen, stack = set(), list(children.get(mid, []))
            while stack:
                c = stack.pop()
                if c in seen:
                    continue
                seen.add(c)
                stack.extend(children.get(c, []))
            return seen

        human_msgs = set()
        have_human_data = False
        if self._caps["has_recipients"]:
            try:
                human_msgs = {
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT msg_id FROM debate_message_recipients "
                        "WHERE lower(recipient) LIKE 'human-%' "
                        "   OR lower(recipient) LIKE 'human\\_%' ESCAPE '\\' "
                        "   OR lower(recipient) IN ('human', 'operator', 'оператор')"
                    ).fetchall()
                }
            except sqlite3.OperationalError:
                human_msgs = set()
            have_human_data = bool(human_msgs)

        op_replied = set()
        for r in rows:
            if r["reply_to"] and _A_IS_OPERATOR_ROLE.match(r["role"] or ""):
                op_replied.add(r["reply_to"])

        now = self._now()
        out, cand = [], 0
        for r in rows:
            if r["kind"] not in ("Q", "DECISION"):
                continue
            if _A_IS_OPERATOR_ROLE.match(r["role"] or ""):
                continue
            dt = parse_ts(r["ts"])
            if dt is None or (now - dt).days > 21:
                continue
            cand += 1
            b = r["body"]
            if have_human_data:
                if r["msg_id"] not in human_msgs:
                    continue
                fwd_txt = "адресиран до оператора (human-)"
            else:
                m = _A_OP_AWAIT.search(b)
                if not m:
                    continue
                s = m.start()
                if _A_REF.search(b[max(0, s - 45) : s + 45]):
                    continue
                fwd_txt = m.group(0)[:60]
            if r["msg_id"] in op_replied:
                continue
            desc = descendants(r["msg_id"])
            if any(
                (byid[c]["ts"] or "") > (r["ts"] or "")
                and (
                    _A_TAKEN.search(byid[c]["body"])
                    or _A_IS_OPERATOR_ROLE.match(byid[c]["role"] or "")
                )
                for c in desc
            ):
                continue
            latest = r["ts"] or ""
            for c in desc:
                t = byid[c]["ts"] or ""
                if t > latest:
                    latest = t
            ldt = parse_ts(latest)
            stale = bool(ldt and (now - ldt).days > 5)
            out.append(
                {
                    "msg_id": r["msg_id"],
                    "role": r["role"],
                    "kind": r["kind"],
                    "priority": r["priority"] or "INFO",
                    "ts": r["ts"],
                    "age": self._rel(r["ts"]),
                    "line": one_line(b),
                    "body": b,
                    "stale": stale,
                    "fwd": fwd_txt,
                }
            )

        # ── Layer 2: live-await (ADOPTION FIX 1) ──────────────────────────
        # Only when live_await; layer-1 output above is byte-identical to board.
        if live_await:
            layer1_ids = {x["msg_id"] for x in out}
            for r in rows:
                if r["msg_id"] in layer1_ids:
                    continue  # dedup vs layer 1
                if r["kind"] not in ("Q", "DECISION", "PING", "STATUS"):
                    continue
                if _A_IS_OPERATOR_ROLE.match(r["role"] or ""):
                    continue  # (v) author not operator
                dt = parse_ts(r["ts"])
                if dt is None or (now - dt).days > 21:
                    continue  # (iv) <=21d
                b = r["body"]
                lead = re.split(r"\n\s*\n", b, maxsplit=1)[0]
                # This must express a present request. Broad keyword matching
                # previously admitted historical receipts and constraints such
                # as "no deploy without operator GO"; searching the full body
                # also admitted quoted evidence and even the hotfix receipt's
                # explanation of "pending operator language". An actionable
                # ask belongs in the leading paragraph.
                m = _A_OP_AWAIT.search(lead)
                if not m:
                    continue  # (ii) operator-await marker in body
                # exclude already-given / already-recorded GO in this very body
                if _A_TAKEN.search(b) or _A_REF.search(
                    lead[max(0, m.start() - 45) : m.start() + 45]
                ):
                    continue
                if r["msg_id"] in op_replied:
                    continue  # (iii) unresolved — no operator reply
                desc = descendants(r["msg_id"])
                if any(
                    (byid[c]["ts"] or "") > (r["ts"] or "")
                    and (
                        _A_TAKEN.search(byid[c]["body"])
                        or _A_OPERATOR_ACTION_RESOLVED.search(byid[c]["body"])
                        or _A_IS_OPERATOR_ROLE.match(byid[c]["role"] or "")
                    )
                    for c in desc
                ):
                    continue  # (iii) unresolved — no later 'taken' in thread
                latest = r["ts"] or ""
                for c in desc:
                    t = byid[c]["ts"] or ""
                    if t > latest:
                        latest = t
                ldt = parse_ts(latest)
                stale = bool(ldt and (now - ldt).days > 5)
                out.append(
                    {
                        "msg_id": r["msg_id"],
                        "role": r["role"],
                        "kind": r["kind"],
                        "priority": r["priority"] or "INFO",
                        "ts": r["ts"],
                        "age": self._rel(r["ts"]),
                        "line": one_line(b),
                        "body": b,
                        "stale": stale,
                        "fwd": f"live-await: {m.group(0)[:48]}",
                    }
                )

        out.sort(key=lambda x: x["ts"] or "", reverse=True)
        # debate/v1 ESCALATE is the authoritative, structured human queue.
        # It is additive to the legacy heuristic layer while old topics exist.
        if self._caps.get("has_human_packets"):
            existing_ids = {item["msg_id"] for item in out}
            packets = con.execute(
                "SELECT m.msg_id,m.role,m.kind,m.priority,"
                "COALESCE(m.ts,m.created_at) AS ts,m.body,p.exact_human_action "
                "FROM debate_human_packets p JOIN debate_messages m ON m.msg_id=p.msg_id "
                "WHERE p.state='open' AND "
                + self._visible_sql("m")
                + " ORDER BY COALESCE(m.ts,m.created_at) DESC"
            ).fetchall()
            for row in packets:
                if row["msg_id"] in existing_ids:
                    continue
                out.append(
                    {
                        "msg_id": row["msg_id"],
                        "role": row["role"],
                        "kind": row["kind"],
                        "priority": row["priority"] or "H",
                        "ts": row["ts"],
                        "age": self._rel(row["ts"]),
                        "line": one_line(row["exact_human_action"]),
                        "body": row["body"] or "",
                        "stale": False,
                        "fwd": "typed ESCALATE packet",
                    }
                )
                existing_ids.add(row["msg_id"])
            out.sort(key=lambda x: x["ts"] or "", reverse=True)
        return out, cand

    def waiting_section_b(self):
        """Browser-board section B: relevant open today/next tasks, read-only."""
        required = {
            "id",
            "title",
            "section",
            "priority",
            "status",
            "due_date",
            "project",
            "updated_at",
            "type",
        }
        if not self._caps["has_tasks"] or not required.issubset(self._caps["tasks"]):
            return [], 0
        con = self._conn
        before = con.execute(
            "SELECT COUNT(*) FROM tasks "
            "WHERE status IN ('not_started','in_progress') "
            "AND section IN ('today','next')"
        ).fetchone()[0]
        today = self._now().date()
        cutoff = today + timedelta(days=21)
        has_versions = (
            self._caps["has_task_field_versions"]
            and {"task_id", "field_name", "updated_order", "source_event_id"}
            <= self._caps["task_field_versions"]
        )
        version_select = (
            ", v.updated_order AS status_order, v.source_event_id AS status_event_id"
            if has_versions
            else ", 0 AS status_order, NULL AS status_event_id"
        )
        version_join = (
            "LEFT JOIN task_field_versions v "
            "ON v.task_id = t.id AND v.field_name = 'status'"
            if has_versions
            else ""
        )
        sql = f"""
        SELECT t.id, t.title, t.section, t.priority, t.status, t.due_date,
               t.project, t.updated_at, t.type{version_select}
        FROM tasks t
        {version_join}
        WHERE t.status IN ('not_started','in_progress')
          AND t.section IN ('today','next')
          AND ( (t.due_date IS NOT NULL AND date(t.due_date) <= date(?))
                OR (t.priority IN ('high','critical') AND t.due_date IS NULL)
                OR (t.section='today' AND t.due_date IS NULL) )
          AND NOT ( t.project='workstation-maintenance'
                    AND t.due_date IS NOT NULL
                    AND date(t.due_date) > date(?) )
        ORDER BY (t.due_date IS NULL), date(t.due_date) ASC,
                 CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END
        """
        items = []
        for row in con.execute(
            sql, (cutoff.isoformat(), cutoff.isoformat())
        ).fetchall():
            items.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "section": row["section"] or "",
                    "priority": row["priority"] or "",
                    "status": row["status"] or "",
                    "type": row["type"] or "task",
                    "due_date": (row["due_date"] or "")[:10],
                    "project": row["project"] or "",
                    "updated_at": row["updated_at"] or "",
                    "status_order": int(row["status_order"] or 0),
                    "status_event_id": row["status_event_id"] or "",
                    "age": self._rel(row["updated_at"]),
                }
            )
        return items, int(before)

    def recent_auto_links(self, *, hours=24, limit=3):
        """Newest high-confidence silent accepts, bounded for operator review.

        These rows are informational and reversible.  Silence keeps the link;
        they are not counted as human training labels.
        """
        if not (
            self._caps["has_link_decisions"]
            and self._caps["has_task_entity_links"]
            and self._caps["has_tasks"]
            and self._caps["has_entities"]
        ):
            return []
        hours = max(1, min(int(hours), 168))
        limit = max(1, min(int(limit), 3))
        cutoff = self._now() - timedelta(hours=hours)
        rows = self._conn.execute(
            "SELECT d.decision_id, d.task_id, d.entity_id, d.score, "
            "d.signals_json, d.model_version, d.updated_at, "
            "t.title AS task_title, e.name AS entity_name "
            "FROM link_suggestion_decisions AS d "
            "JOIN tasks AS t ON t.id = d.task_id "
            "JOIN entities AS e ON e.id = d.entity_id "
            "JOIN task_entity_links AS tel "
            "  ON tel.task_id = d.task_id AND tel.entity_id = d.entity_id "
            "WHERE d.decision = 'accepted' "
            "AND d.decision_source = 'auto_high_confidence' "
            "AND tel.link_type = 'auto_high_confidence' "
            "AND datetime(d.updated_at) >= datetime(?) "
            "ORDER BY COALESCE(d.score, 0) DESC, d.updated_at DESC, d.decision_id "
            "LIMIT ?",
            (cutoff.isoformat(), limit),
        ).fetchall()
        items = []
        for row in rows:
            try:
                receipt = json.loads(row["signals_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                receipt = {}
            items.append(
                {
                    "id": f"link:{row['decision_id']}",
                    "decision_id": row["decision_id"],
                    "task_id": row["task_id"],
                    "task_title": row["task_title"],
                    "entity_id": int(row["entity_id"]),
                    "entity_name": row["entity_name"],
                    "score": float(row["score"] or 0.0),
                    "reasons": list(receipt.get("reasons") or [])[:3],
                    "model_version": row["model_version"],
                    "updated_at": row["updated_at"],
                    "age": self._rel(row["updated_at"]),
                    "reversible": True,
                }
            )
        return items

    # ---- VIEW 2: recent (board.py:466-521) ----------------------------------
    def recent(self, hours, role, kinds):
        if not self._caps["has_debate"]:
            return {"items": [], "hours": hours, "count": 0, "roles": [], "kinds": []}
        con = self._conn
        cutoff = self._now().timestamp() - hours * 3600
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        kinds = [k for k in (kinds or []) if k] or [
            "DECISION",
            "STATE",
            "STATUS",
            "VERIFY",
            "CONCEDE",
            "DISSENT",
            "ESCALATE",
        ]
        qm = ",".join("?" * len(kinds))
        args = list(kinds)
        role_clause = ""
        if role:
            role_clause = " AND m.role = ?"
            args.append(role)
        args.append(cutoff_iso)
        sql = (
            "SELECT m.msg_id, m.role, m.kind, m.priority, "
            "COALESCE(m.ts, m.created_at) AS ts, m.topic_id, m.body "
            f"FROM debate_messages m WHERE m.kind IN ({qm}){role_clause} "
            "AND " + self._visible_sql("m") + " "
            "AND COALESCE(m.ts, m.created_at) >= ? ORDER BY ts DESC LIMIT 200"
        )
        items = []
        for r in con.execute(sql, args).fetchall():
            items.append(
                {
                    "msg_id": r["msg_id"],
                    "role": r["role"],
                    "kind": r["kind"],
                    "priority": r["priority"] or "",
                    "ts": r["ts"],
                    "age": self._rel(r["ts"]),
                    "topic_id": r["topic_id"],
                    "line": one_line(r["body"]),
                    "body": r["body"] or "",
                }
            )
        roles = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT role FROM debate_messages "
                "WHERE role IS NOT NULL ORDER BY role"
            ).fetchall()
        ]
        return {
            "items": items,
            "hours": hours,
            "count": len(items),
            "roles": roles,
            "kinds": kinds,
        }

    # ---- VIEW 3: topics + topic_thread (board.py:524-619) -------------------
    def topics(self):
        if not self._caps["has_debate"]:
            return {"topics": [], "count": 0}
        con = self._conn
        counts = {
            r["topic_id"]: r["c"]
            for r in con.execute(
                "SELECT m.topic_id, COUNT(*) c FROM debate_messages m "
                "WHERE m.kind != 'WATERMARK' AND "
                + self._visible_sql("m")
                + " GROUP BY m.topic_id"
            ).fetchall()
        }
        last = {
            r["topic_id"]: r["mx"]
            for r in con.execute(
                "SELECT m.topic_id, MAX(COALESCE(m.ts,m.created_at)) mx "
                "FROM debate_messages m WHERE "
                + self._visible_sql("m")
                + " GROUP BY m.topic_id"
            ).fetchall()
        }
        out, seen = [], set()
        protocol = {}
        if self._caps.get("has_protocol_state"):
            protocol = {
                row["topic_id"]: dict(row)
                for row in con.execute(
                    "SELECT topic_id,protocol_version,phase,round_no,max_rounds,"
                    "stalemate_reason FROM debate_protocol_state"
                ).fetchall()
            }
        if self._caps["has_debates_tbl"]:
            for r in con.execute(
                "SELECT topic_id, title, state, created_at FROM debates"
            ).fetchall():
                tid = r["topic_id"]
                seen.add(tid)
                item = {
                    "topic_id": tid,
                    "title": r["title"] or tid,
                    "state": r["state"] or "",
                    "count": counts.get(tid, 0),
                    "last_ts": last.get(tid),
                    "age": self._rel(last.get(tid)),
                }
                if tid in protocol:
                    item.update(protocol[tid])
                out.append(item)
        for tid in counts:
            if tid not in seen:
                item = {
                    "topic_id": tid,
                    "title": tid,
                    "state": "",
                    "count": counts.get(tid, 0),
                    "last_ts": last.get(tid),
                    "age": self._rel(last.get(tid)),
                }
                if tid in protocol:
                    item.update(protocol[tid])
                out.append(item)
        out.sort(key=lambda x: x["last_ts"] or "", reverse=True)
        return {"topics": out, "count": len(out)}

    def topic_thread(self, topic_id):
        if not topic_id or not self._caps["has_debate"]:
            return {
                "topic_id": topic_id,
                "title": topic_id,
                "state": "",
                "count": 0,
                "messages": [],
            }
        con = self._conn
        title, state = topic_id, ""
        if self._caps["has_debates_tbl"]:
            row = con.execute(
                "SELECT title, state FROM debates WHERE topic_id=?", (topic_id,)
            ).fetchone()
            if row:
                title = row["title"] or topic_id
                state = row["state"] or ""
        protocol_columns = ""
        if {
            "protocol_version",
            "round_no",
            "body_mode",
            "payload_json",
        } <= self._caps.get("debate_messages", set()):
            protocol_columns = (
                ", m.protocol_version, m.round_no, m.body_mode, m.payload_json"
            )
        rows = con.execute(
            "SELECT m.msg_id, m.role, m.kind, m.priority, COALESCE(m.ts, m.created_at) AS ts, "
            "m.reply_to, m.body"
            + protocol_columns
            + " FROM debate_messages m WHERE m.topic_id=? AND m.kind != 'WATERMARK' AND "
            + self._visible_sql("m")
            + " ORDER BY COALESCE(m.ts, m.created_at) ASC",
            (topic_id,),
        ).fetchall()
        msgs = []
        for r in rows:
            item = {
                "msg_id": r["msg_id"],
                "role": r["role"],
                "kind": r["kind"],
                "priority": r["priority"] or "",
                "ts": r["ts"],
                "age": self._rel(r["ts"]),
                "reply_to": r["reply_to"],
                "line": one_line(r["body"]),
                "body": r["body"] or "",
            }
            if protocol_columns:
                item.update(
                    {
                        "protocol_version": r["protocol_version"],
                        "round_no": r["round_no"],
                        "body_mode": r["body_mode"],
                        "payload_json": r["payload_json"],
                    }
                )
            msgs.append(item)
        result = {
            "topic_id": topic_id,
            "title": title,
            "state": state,
            "count": len(msgs),
            "messages": msgs,
        }
        if self._caps.get("has_protocol_state"):
            protocol = con.execute(
                "SELECT protocol_version,phase,round_no,max_rounds,"
                "blind_barrier_state,stalemate_reason,phase_deadline_at "
                "FROM debate_protocol_state WHERE topic_id=?",
                (topic_id,),
            ).fetchone()
            if protocol:
                result["protocol_state"] = dict(protocol)
        return result

    # ---- grouped per-source BM25 search (board.py:622-748; M4 verbatim) ------
    def _debate_index(self):
        if self._debate_mem is not None:
            return self._debate_mem
        mem = sqlite3.connect(":memory:")
        mem.row_factory = sqlite3.Row
        mem.execute(
            "CREATE VIRTUAL TABLE d USING fts5("
            "msg_id UNINDEXED, topic_id UNINDEXED, role, kind, ts UNINDEXED, "
            "priority UNINDEXED, body, tokenize='unicode61 remove_diacritics 2')"
        )
        rows = self._conn.execute(
            "SELECT msg_id, COALESCE(topic_id,''), COALESCE(role,''), "
            "COALESCE(kind,''), COALESCE(ts, created_at, ''), "
            "COALESCE(priority,''), COALESCE(body,'') FROM debate_messages m WHERE "
            + self._visible_sql("m")
        ).fetchall()
        mem.executemany(
            "INSERT INTO d(msg_id,topic_id,role,kind,ts,priority,body) VALUES(?,?,?,?,?,?,?)",
            [tuple(r) for r in rows],
        )
        mem.commit()
        self._debate_mem = mem
        return mem

    def board_search(self, query, limit=25):
        query = (query or "").strip()
        result = {"query": query, "debate": [], "tasks": [], "knowledge": []}
        if not query:
            return result
        # debate — in-memory FTS mirror, pure bm25 (verbatim board; no recency)
        if self._caps["has_debate"]:
            mem = self._debate_index()
            hits = []
            expr = fts_expr(query, "and")
            if expr:
                try:
                    hits = mem.execute(
                        "SELECT msg_id, topic_id, role, kind, ts, priority, "
                        "snippet(d, 6, '〈', '〉', ' … ', 12) AS snip, body, "
                        "bm25(d) AS score FROM d WHERE d MATCH ? ORDER BY score LIMIT ?",
                        (expr, limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    hits = []
                if not hits:
                    try:
                        hits = mem.execute(
                            "SELECT msg_id, topic_id, role, kind, ts, priority, "
                            "snippet(d, 6, '〈', '〉', ' … ', 12) AS snip, body, "
                            "bm25(d) AS score FROM d WHERE d MATCH ? ORDER BY score LIMIT ?",
                            (fts_expr(query, "or"), limit),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        hits = []
            for r in hits:
                result["debate"].append(
                    {
                        "msg_id": r["msg_id"],
                        "role": r["role"],
                        "kind": r["kind"],
                        "topic_id": r["topic_id"],
                        "ts": r["ts"],
                        "age": self._rel(r["ts"]),
                        "snippet": r["snip"] or one_line(r["body"]),
                        "body": r["body"] or "",
                        "score": round(r["score"], 3),
                    }
                )
        if self._caps["has_tasks_fts"]:
            result["tasks"] = self._fts_tasks(query, limit)
        if self._caps["has_memory_fts"]:
            result["knowledge"] = self._fts_memory(query, limit)
        return result

    def _fts_tasks(self, query, limit):
        tcols = self._caps["tasks"]
        has_versions = (
            self._caps["has_task_field_versions"]
            and {"task_id", "field_name", "updated_order", "source_event_id"}
            <= self._caps["task_field_versions"]
        )
        version_select = (
            "v.updated_order AS status_order, v.source_event_id AS status_event_id,"
            if has_versions
            else "0 AS status_order, NULL AS status_event_id,"
        )
        version_join = (
            " LEFT JOIN task_field_versions v"
            " ON v.task_id = t.id AND v.field_name = 'status'"
            if has_versions
            else ""
        )
        for mode in ("and", "or"):
            expr = fts_expr(query, mode)
            if not expr:
                return []
            try:
                rows = self._conn.execute(
                    "SELECT t.id, t.title, "
                    + ("t.type," if "type" in tcols else "'' AS type,")
                    + ("t.section," if "section" in tcols else "'' AS section,")
                    + ("t.status," if "status" in tcols else "'' AS status,")
                    + ("t.project," if "project" in tcols else "'' AS project,")
                    + version_select
                    + " snippet(tasks_fts,1,'〈','〉',' … ',12) AS snip,"
                    + " bm25(tasks_fts) AS score"
                    + " FROM tasks_fts JOIN tasks t ON t.rowid = tasks_fts.rowid"
                    + version_join
                    + " WHERE tasks_fts MATCH ? ORDER BY score LIMIT ?",
                    (expr, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                return [
                    {
                        "id": r["id"],
                        "title": r["title"],
                        "type": r["type"],
                        "section": r["section"],
                        "status": r["status"],
                        "project": r["project"],
                        "snippet": r["snip"],
                        "status_order": int(r["status_order"] or 0),
                        "status_event_id": r["status_event_id"] or "",
                        "score": round(r["score"], 3),
                    }
                    for r in rows
                ]
        return []

    def _fts_memory(self, query, limit):
        for mode in ("and", "or"):
            expr = fts_expr(query, mode)
            if not expr:
                return []
            try:
                rows = self._conn.execute(
                    "SELECT e.id AS eid, COALESCE(e.name, memory_fts.name) AS name, "
                    "COALESCE(e.entity_type, memory_fts.entity_type) AS etype, "
                    "COALESCE(e.project,'') AS project, "
                    "snippet(memory_fts,2,'〈','〉',' … ',14) AS snip, "
                    "bm25(memory_fts) AS score "
                    "FROM memory_fts LEFT JOIN entities e ON e.id = memory_fts.rowid "
                    "WHERE memory_fts MATCH ? ORDER BY score LIMIT ?",
                    (expr, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                return [
                    {
                        "id": r["eid"],
                        "name": r["name"],
                        "type": r["etype"] or "",
                        "project": r["project"] or "",
                        "snippet": r["snip"],
                        "score": round(r["score"], 3),
                    }
                    for r in rows
                ]
        return []
