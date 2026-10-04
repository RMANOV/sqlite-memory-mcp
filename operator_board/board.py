#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Operator Board — null-floor (memory branch-B MVP)
=================================================

Персистентно локално табло над ~/.claude/memory/memory.db, което решава
операторската болка: "конзолата се залива, не мога да намеря нещо отпреди час".

ПРИНЦИПИ (по конструкция):
  * READ-ONLY: базата се отваря само с mode=ro (file:...?mode=ro, uri=True).
    Нула запис, нула schema промени, нула DML в продукционната база.
  * NO-LLM: ранкирането е ЧИСТО механично — recency + FTS5 BM25. Нула
    embeddings, нула predictor, нула модел. Това е null-floor базовата линия.
  * Локален личен инструмент: сервира само на 127.0.0.1. Нула mребежен изход.

Стартиране (една команда):
    python3 board.py           # → http://127.0.0.1:8787
    python3 board.py --port 9000 --db /path/to/memory.db

Стек: само stdlib (http.server + sqlite3). Flask не е необходим.
"""
from __future__ import annotations

import argparse
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Canonical CAS status-transition adapter (repo root, one dir above this file).
# Guarded so the read-only board still starts if the adapter is absent; the
# /api/task-status write route then fail-closes with 503. NO subprocess, NO
# persistent write connection in this process — the adapter opens its own
# BEGIN IMMEDIATE connection per operation.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from task_status_cas import StatusToken, StatusSingleFlight, transition_status
    _CAS_IMPORT_ERR = None
except Exception as _e:  # pragma: no cover - environment guard
    StatusToken = StatusSingleFlight = transition_status = None
    _CAS_IMPORT_ERR = str(_e)

DEFAULT_DB = os.path.expanduser("~/.claude/memory/memory.db")
DEFAULT_PORT = 8787
HOST = "127.0.0.1"

# Множество roles, третирани като "изпълнители" (за да покажем КОЙ чака).
_PRIORITY_ORDER = {"H": 0, "M": 1, "L": 2, "INFO": 3}


# --------------------------------------------------------------------------- #
#  READ-ONLY достъп до базата
# --------------------------------------------------------------------------- #
class DB:
    """Тънка обвивка — всяка връзка е строго read-only (mode=ro)."""

    def __init__(self, path: str):
        self.path = path
        if not os.path.exists(path):
            raise SystemExit(f"[FATAL] Базата не съществува: {path}")
        self.uri = f"file:{path}?mode=ro"
        # Интроспекция на схемата ЕДНОКРАТНО — адаптираме се, не приемаме наизуст.
        self.caps = self._introspect()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.uri, uri=True, check_same_thread=False, timeout=5.0)
        con.row_factory = sqlite3.Row
        return con

    def _introspect(self) -> dict:
        con = self.connect()
        try:
            names = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
            }

            def cols(t: str) -> set:
                if t not in names:
                    return set()
                return {r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()}

            return {
                "tables": names,
                "debate_messages": cols("debate_messages"),
                "tasks": cols("tasks"),
                "debates": cols("debates"),
                "has_recipients": "debate_message_recipients" in names,
                "has_tasks_fts": "tasks_fts" in names,
                "has_memory_fts": "memory_fts" in names,
                "has_debate": "debate_messages" in names,
                "has_tasks": "tasks" in names,
                "has_debates_tbl": "debates" in names,
            }
        finally:
            con.close()


# --------------------------------------------------------------------------- #
#  In-memory FTS5 огледало на debate_messages (няма debate_fts в прод базата).
#  Строи се от read-only snapshot; refresh по TTL, за да хваща нови съобщения.
# --------------------------------------------------------------------------- #
class DebateIndex:
    TTL_SECONDS = 45

    def __init__(self, db: DB):
        self.db = db
        self._lock = threading.Lock()
        self._mem: sqlite3.Connection | None = None
        self._built_at = 0.0
        self._count = 0

    def _rebuild(self) -> None:
        mem = sqlite3.connect(":memory:", check_same_thread=False)
        mem.row_factory = sqlite3.Row
        mem.execute(
            "CREATE VIRTUAL TABLE d USING fts5("
            "msg_id UNINDEXED, topic_id UNINDEXED, role, kind, ts UNINDEXED, "
            "priority UNINDEXED, body, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        src = self.db.connect()
        try:
            rows = src.execute(
                "SELECT msg_id, COALESCE(topic_id,''), COALESCE(role,''), "
                "COALESCE(kind,''), COALESCE(ts, created_at, ''), "
                "COALESCE(priority,''), COALESCE(body,'') FROM debate_messages"
            ).fetchall()
        finally:
            src.close()
        mem.executemany(
            "INSERT INTO d(msg_id,topic_id,role,kind,ts,priority,body) "
            "VALUES(?,?,?,?,?,?,?)",
            [tuple(r) for r in rows],
        )
        mem.commit()
        old = self._mem
        self._mem = mem
        self._built_at = datetime.now().timestamp()
        self._count = len(rows)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def _ensure_fresh(self) -> None:
        now = datetime.now().timestamp()
        if self._mem is None or (now - self._built_at) > self.TTL_SECONDS:
            self._rebuild()

    def search(self, match_expr: str, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            self._ensure_fresh()
            assert self._mem is not None
            return self._mem.execute(
                "SELECT msg_id, topic_id, role, kind, ts, priority, "
                "snippet(d, 6, '〈', '〉', ' … ', 12) AS snip, "
                "body, bm25(d) AS score "
                "FROM d WHERE d MATCH ? ORDER BY score LIMIT ?",
                (match_expr, limit),
            ).fetchall()


# --------------------------------------------------------------------------- #
#  Помощни функции
# --------------------------------------------------------------------------- #
def parse_ts(ts: str | None) -> datetime | None:
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


def rel_bg(ts: str | None) -> str:
    """Относително време на български: 'преди 12 мин', 'преди 3 часа'..."""
    dt = parse_ts(ts)
    if dt is None:
        return "—"
    delta = datetime.now(timezone.utc) - dt
    sec = int(delta.total_seconds())
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


def one_line(body: str | None, n: int = 140) -> str:
    """Първият смислен ред — сигналът, не целият шум."""
    if not body:
        return ""
    for raw in body.splitlines():
        line = raw.strip()
        if line:
            return line[:n] + ("…" if len(line) > n else "")
    return body.strip()[:n]


def fts_expr(query: str, mode: str = "and") -> str:
    """Санитизирано FTS5 prefix-търсене. Пази кирилица/латиница/цифри,
    маха FTS оператори. mode='and' → AND; 'or' → OR (fallback за recall)."""
    toks = [t for t in re.findall(r"\w+", query, re.UNICODE) if t]
    if not toks:
        return ""
    parts = [f'"{t}"*' for t in toks]
    joiner = " OR " if mode == "or" else " "
    return joiner.join(parts)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
#  Section A — "Решения, чакащи ТЕБ": ЧИСТО regex, no-LLM, HIGH-PRECISION.
#  Дебатът е machine↔machine → повечето „operator" споменавания са INTER-AGENT
#  ШУМ: CONDUCTOR STATUS/DECISION broadcasts, executor ACK-и (kind=A), референции
#  към ВЕЧЕ дадено GO, вече-решени гейтове. Едно съобщение ГЕНУИННО чака ЧОВЕКА
#  само ако е ВСИЧКО от:  (i) kind ∈ {Q, DECISION};  (ii) адресиран до оператора
#  — PRIMARY: recipient с 'human-' префикс; иначе FALLBACK: явен operator-decision
#  REQUEST маркер + агентска роля;  (iii) нерешен (няма operator-отговор/решение
#  по-късно);  (iv) възраст ≤ 21 дни;  (v) авторът не е самият оператор.
#  Проектен принцип: ПРЕДПОЧИТАЙ 0 пред шум.
# --------------------------------------------------------------------------- #
# Референция към ВЕЧЕ даден/записан/изпълнен GO (близо до маркера) → НЕ е заявка.
_A_REF = re.compile(
    r"(записан|получен|дошъл|даде|даден|дадено|взето|заключен|landed|recorded|"
    r"gave|given|granted|verbatim|давай|\bACK\b|per\s+operator|"
    r"по\s+операторск\w*\s+директив|availability\s+override|поеми|разпоред|"
    r"standing=|DECISION\s+brief|→\s*ADVOCATE)",
    re.I,
)
# По-късен post в нишката, който отбелязва ВЗЕТО операторско решение → нишката е решена.
_A_TAKEN = re.compile(
    r"(операторск\w*\s+(?:GO|решени\w+)\s+(?:записан|получен|даден|взето)|"
    r"оператор\w*\s+(?:даде|потвърди|реши|одобри|нареди|разпореди)|"
    r"operator\s+(?:gave|approved|confirmed|decided)|давай|verbatim\s+GO|ЗАПИСАН)",
    re.I,
)
# HIGH-PRECISION fallback (само когато липсват чисти recipient 'human-' данни):
# тялото ЯВНО ИСКА операторско решение/GO ТЕПЪРВА — не записва вече-взето, не
# описва къде „остават"/„блокират" неща. Умишлено ТЕСЕН (без двусмислените
# „остава…оператор"/„блокира…оператор"), защото 0 > шум.
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
# Ролята на самия ЧОВЕК-оператор — за да го изключим като автор (v) и да разпознаем
# негови отговори (iii). В текущата база не се среща → предикатът е future-proof.
_A_IS_OPERATOR_ROLE = re.compile(r"^\s*(human|operator|оператор)", re.I)


# --------------------------------------------------------------------------- #
#  Логика на трите view-а + търсачка
# --------------------------------------------------------------------------- #
class Board:
    def __init__(self, db: DB, enable_writes: bool = False):
        self.db = db
        self.debate_idx = DebateIndex(db) if db.caps["has_debate"] else None
        # Read-only by default. The narrow status-write route is armed ONLY
        # when the operator launches with --enable-writes; otherwise the board
        # stays strictly read-only (mode=ro) and /api/task-status fail-closes.
        self.enable_writes = bool(enable_writes)
        self._db_path = db.path

    # ---- VIEW 1: "Какво чака мен" (2 operator-facing секции) -------------- #
    def waiting(self) -> dict:
        con = self.db.connect()
        try:
            a_items, a_before = (
                self._section_a(con) if self.db.caps["has_debate"] else ([], 0)
            )
            b_items, b_before = (
                self._section_b(con) if self.db.caps["has_tasks"] else ([], 0)
            )
            return {
                "generated_at": now_utc_iso(),
                # Секция A — решения, чакащи ТЕБ (debate, само human-reaction)
                "section_a": a_items,
                "section_a_count": len(a_items),
                "section_a_before": a_before,
                # Секция B — твои задачи (сега)
                "section_b": b_items,
                "section_b_count": len(b_items),
                "section_b_before": b_before,
            }
        finally:
            con.close()

    def _section_a(self, con):
        """Debate съобщения, които ГЕНУИННО чакат ЧОВЕКА-оператор — no-LLM, high-precision.

        Тесен предикат (ВСИЧКИ трябва да са изпълнени — виж коментара при _A_OP_AWAIT):
          (i) kind ∈ {Q, DECISION}; (ii) адресиран до оператора; (iii) нерешен;
          (iv) възраст ≤ 21 дни; (v) авторът не е операторът. Предпочитаме 0 пред шум.
        """
        rows = con.execute(
            "SELECT msg_id, role, kind, priority, "
            "COALESCE(ts, created_at) AS ts, reply_to, COALESCE(body,'') AS body "
            "FROM debate_messages"
        ).fetchall()
        byid = {r["msg_id"]: r for r in rows}
        children: dict = {}
        for r in rows:
            if r["reply_to"]:
                children.setdefault(r["reply_to"], []).append(r["msg_id"])

        def descendants(mid: str) -> set:
            seen, stack = set(), list(children.get(mid, []))
            while stack:
                c = stack.pop()
                if c in seen:
                    continue
                seen.add(c)
                stack.extend(children.get(c, []))
            return seen

        # (ii) PRIMARY сигнал — msg_id-та адресирани до оператора (recipient 'human-'
        #      префикс). Ако таблицата не носи такива редове → fallback към маркер.
        human_msgs: set = set()
        have_human_data = False
        if self.db.caps["has_recipients"]:
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

        # (iii) msg_id-та, на които САМИЯТ оператор (human-/operator роля) е отговорил.
        op_replied: set = set()
        for r in rows:
            if r["reply_to"] and _A_IS_OPERATOR_ROLE.match(r["role"] or ""):
                op_replied.add(r["reply_to"])

        now = datetime.now(timezone.utc)
        out = []
        cand = 0
        for r in rows:
            # (i) kind — само генуинни въпроси/решения (изключва STATUS/A/PING/WATERMARK).
            if r["kind"] not in ("Q", "DECISION"):
                continue
            # (v) авторът да НЕ е операторът.
            if _A_IS_OPERATOR_ROLE.match(r["role"] or ""):
                continue
            # (iv) възраст ≤ 21 дни.
            dt = parse_ts(r["ts"])
            if dt is None or (now - dt).days > 21:
                continue
            cand += 1  # raw pool за диагностика (filtered N->M)
            b = r["body"]
            # (ii) адресиран до оператора — PRIMARY recipient, иначе HIGH-PRECISION маркер.
            if have_human_data:
                if r["msg_id"] not in human_msgs:
                    continue
                fwd_txt = "адресиран до оператора (human-)"
            else:
                m = _A_OP_AWAIT.search(b)
                if not m:
                    continue
                s = m.start()
                # изключи РЕФЕРЕНЦИЯ към вече дадено/записано решение до маркера.
                if _A_REF.search(b[max(0, s - 45):s + 45]):
                    continue
                fwd_txt = m.group(0)[:60]
            # (iii) нерешен — нито operator-отговор, нито по-късно ВЗЕТО решение в нишката.
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
                    "age": rel_bg(r["ts"]),
                    "line": one_line(b),
                    "body": b,
                    "stale": stale,
                    "fwd": fwd_txt,
                }
            )
        out.sort(key=lambda x: x["ts"] or "", reverse=True)  # newest first
        # `cand` = raw pool (kind∈{Q,DECISION} · агент · ≤21д) преди tight-филтъра.
        return out, cand

    def _section_b(self, con):
        """Твои задачи (сега): today/next, отворени, с релевантен времеви прозорец."""
        before = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('not_started','in_progress') "
            "AND section IN ('today','next')"
        ).fetchone()[0]
        # t.type + status version token (updated_order, source_event_id) are
        # exposed so the client can carry the exact expected-CAS token back on
        # POST /api/task-status; a foreign status change between render and
        # click then fails the CAS predicate (fail-closed).
        sql = """
        SELECT t.id, t.title, t.section, t.priority, t.status, t.due_date,
               t.project, t.updated_at, t.type,
               v.updated_order AS status_order, v.source_event_id AS status_event_id
        FROM tasks t
        LEFT JOIN task_field_versions v
               ON v.task_id = t.id AND v.field_name = 'status'
        WHERE t.status IN ('not_started','in_progress')
          AND t.section IN ('today','next')
          AND ( (t.due_date IS NOT NULL AND date(t.due_date) <= date('now','+21 day'))
                OR (t.priority IN ('high','critical') AND t.due_date IS NULL)
                OR (t.section='today' AND t.due_date IS NULL) )
          AND NOT ( t.project='workstation-maintenance'
                    AND t.due_date IS NOT NULL
                    AND date(t.due_date) > date('now','+21 day') )
        ORDER BY (t.due_date IS NULL), date(t.due_date) ASC,
                 CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                               WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END
        """
        items = [
            {
                "id": r["id"],
                "title": r["title"],
                "section": r["section"] or "",
                "priority": r["priority"] or "",
                "status": r["status"] or "",
                "due_date": (r["due_date"] or "")[:10],
                "project": r["project"] or "",
                "updated_at": r["updated_at"] or "",
                "age": rel_bg(r["updated_at"]),
                "type": r["type"] or "task",
                "status_order": r["status_order"],
                "status_event_id": r["status_event_id"],
            }
            for r in con.execute(sql).fetchall()
        ]
        return items, before

    # ---- VIEW 2: "Какво реши X преди час" -------------------------------- #
    def recent(self, hours: float, role: str | None, kinds: list[str]) -> dict:
        con = self.db.connect()
        try:
            if not self.db.caps["has_debate"]:
                return {"items": [], "hours": hours, "count": 0}
            cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
            cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
            kinds = [k for k in kinds if k] or ["DECISION", "STATE", "STATUS"]
            qm = ",".join("?" * len(kinds))
            args: list = list(kinds)
            role_clause = ""
            if role:
                role_clause = " AND m.role = ?"
                args.append(role)
            args.append(cutoff_iso)
            sql = f"""
            SELECT m.msg_id, m.role, m.kind, m.priority,
                   COALESCE(m.ts, m.created_at) AS ts, m.topic_id, m.body
            FROM debate_messages m
            WHERE m.kind IN ({qm}){role_clause}
              AND COALESCE(m.ts, m.created_at) >= ?
            ORDER BY ts DESC
            LIMIT 200
            """
            items = []
            for r in con.execute(sql, args).fetchall():
                items.append(
                    {
                        "msg_id": r["msg_id"],
                        "role": r["role"],
                        "kind": r["kind"],
                        "priority": r["priority"] or "",
                        "ts": r["ts"],
                        "age": rel_bg(r["ts"]),
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
        finally:
            con.close()

    # ---- VIEW 3: "Целият дебат по тема" ---------------------------------- #
    def topics(self) -> dict:
        con = self.db.connect()
        try:
            out = []
            counts = {
                r["topic_id"]: r["c"]
                for r in con.execute(
                    "SELECT topic_id, COUNT(*) c FROM debate_messages "
                    "WHERE kind != 'WATERMARK' GROUP BY topic_id"
                ).fetchall()
            }
            last = {
                r["topic_id"]: r["mx"]
                for r in con.execute(
                    "SELECT topic_id, MAX(COALESCE(ts,created_at)) mx "
                    "FROM debate_messages GROUP BY topic_id"
                ).fetchall()
            }
            seen = set()
            if self.db.caps["has_debates_tbl"]:
                for r in con.execute(
                    "SELECT topic_id, title, state, created_at FROM debates"
                ).fetchall():
                    tid = r["topic_id"]
                    seen.add(tid)
                    out.append(
                        {
                            "topic_id": tid,
                            "title": r["title"] or tid,
                            "state": r["state"] or "",
                            "count": counts.get(tid, 0),
                            "last_ts": last.get(tid),
                            "age": rel_bg(last.get(tid)),
                        }
                    )
            # Теми без ред в debates (fallback).
            for tid in counts:
                if tid not in seen:
                    out.append(
                        {
                            "topic_id": tid,
                            "title": tid,
                            "state": "",
                            "count": counts.get(tid, 0),
                            "last_ts": last.get(tid),
                            "age": rel_bg(last.get(tid)),
                        }
                    )
            out.sort(key=lambda x: (x["last_ts"] or ""), reverse=True)
            return {"topics": out, "count": len(out)}
        finally:
            con.close()

    def topic_thread(self, topic_id: str) -> dict:
        con = self.db.connect()
        try:
            title = topic_id
            state = ""
            if self.db.caps["has_debates_tbl"]:
                row = con.execute(
                    "SELECT title, state FROM debates WHERE topic_id=?", (topic_id,)
                ).fetchone()
                if row:
                    title = row["title"] or topic_id
                    state = row["state"] or ""
            rows = con.execute(
                "SELECT msg_id, role, kind, priority, "
                "COALESCE(ts, created_at) AS ts, reply_to, body "
                "FROM debate_messages WHERE topic_id=? AND kind != 'WATERMARK' "
                "ORDER BY COALESCE(ts, created_at) ASC",
                (topic_id,),
            ).fetchall()
            msgs = []
            for r in rows:
                msgs.append(
                    {
                        "msg_id": r["msg_id"],
                        "role": r["role"],
                        "kind": r["kind"],
                        "priority": r["priority"] or "",
                        "ts": r["ts"],
                        "age": rel_bg(r["ts"]),
                        "reply_to": r["reply_to"],
                        "line": one_line(r["body"]),
                        "body": r["body"] or "",
                    }
                )
            return {
                "topic_id": topic_id,
                "title": title,
                "state": state,
                "count": len(msgs),
                "messages": msgs,
            }
        finally:
            con.close()

    # ---- Глобална търсачка (debate + tasks/notes + knowledge) ------------ #
    def search(self, query: str, limit: int = 25) -> dict:
        query = (query or "").strip()
        result = {"query": query, "debate": [], "tasks": [], "knowledge": []}
        if not query:
            return result

        # --- debate (in-memory FTS5 mirror) ---
        if self.debate_idx is not None:
            expr = fts_expr(query, "and")
            hits = []
            if expr:
                try:
                    hits = self.debate_idx.search(expr, limit)
                except sqlite3.OperationalError:
                    hits = []
                if not hits:  # OR fallback за по-широк recall
                    try:
                        hits = self.debate_idx.search(fts_expr(query, "or"), limit)
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
                        "age": rel_bg(r["ts"]),
                        "snippet": r["snip"] or one_line(r["body"]),
                        "body": r["body"] or "",
                        "score": round(r["score"], 3),
                    }
                )

        con = self.db.connect()
        try:
            # --- tasks + notes (tasks_fts, вкл. type='note') ---
            if self.db.caps["has_tasks_fts"]:
                result["tasks"] = self._fts_tasks(con, query, limit)
            # --- knowledge (memory_fts → entities/observations) ---
            if self.db.caps["has_memory_fts"]:
                result["knowledge"] = self._fts_memory(con, query, limit)
        finally:
            con.close()
        return result

    def _fts_tasks(self, con, query: str, limit: int) -> list:
        for mode in ("and", "or"):
            expr = fts_expr(query, mode)
            if not expr:
                return []
            try:
                rows = con.execute(
                    "SELECT t.id, t.title, "
                    + ("t.type," if "type" in self.db.caps["tasks"] else "'' AS type,")
                    + (
                        "t.section,"
                        if "section" in self.db.caps["tasks"]
                        else "'' AS section,"
                    )
                    + (
                        "t.status,"
                        if "status" in self.db.caps["tasks"]
                        else "'' AS status,"
                    )
                    + (
                        "t.project,"
                        if "project" in self.db.caps["tasks"]
                        else "'' AS project,"
                    )
                    + " snippet(tasks_fts,1,'〈','〉',' … ',12) AS snip,"
                    + " bm25(tasks_fts) AS score"
                    + " FROM tasks_fts JOIN tasks t ON t.rowid = tasks_fts.rowid"
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
                        "score": round(r["score"], 3),
                    }
                    for r in rows
                ]
        return []

    def _fts_memory(self, con, query: str, limit: int) -> list:
        for mode in ("and", "or"):
            expr = fts_expr(query, mode)
            if not expr:
                return []
            try:
                rows = con.execute(
                    "SELECT e.id AS eid, "
                    "COALESCE(e.name, memory_fts.name) AS name, "
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

    # Операторското затваряне вече минава през тесния POST /api/task-status
    # (CAS status->done през task_status_cas.transition_status). Старият
    # subprocess archive helper (close_task.py) е ПРЕМАХНАТ — нула subprocess
    # write path в този процес (dispatch 5d4cc16c54ee §4 + gate B static proof).
    # /api/close остава 405 containment sentinel.
    def task_status(self, payload: dict) -> dict:
        """Apply ONE CAS-guarded status transition for a Section-B task/note.

        payload: {id, action('done'|'undo'), expected_status, expected_order,
                  expected_event_id, previous_status (undo only)}. Returns a
        JSON-able dict; never raises for caller-fixable input. Fail-closed on:
        adapter missing, writes disabled, unknown id, type not in
        {task,note}, invalid/missing version token. Idempotent when the row
        already sits at the intended target (double-click / retry).
        """
        if transition_status is None or StatusToken is None:
            return {"ok": False, "outcome": "unavailable",
                    "reason": _CAS_IMPORT_ERR or "cas_adapter_missing"}
        if not self.enable_writes:
            return {"ok": False, "outcome": "writes_disabled",
                    "reason": "board launched read-only (no --enable-writes)"}
        tid = str(payload.get("id") or "").strip()
        action = str(payload.get("action") or "done").strip()
        if not tid:
            return {"ok": False, "outcome": "conflict", "reason": "no_id"}
        if action not in ("done", "undo"):
            return {"ok": False, "outcome": "conflict", "reason": "bad_action"}
        # Type fence via the read-only connection (NO write conn opened here;
        # the adapter opens its own BEGIN IMMEDIATE connection).
        con = self.db.connect()
        try:
            row = con.execute(
                "SELECT id, type, status FROM tasks WHERE id=?", (tid,)
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return {"ok": False, "outcome": "conflict", "reason": "not_found"}
        if (row["type"] or "task") not in ("task", "note"):
            return {"ok": False, "outcome": "conflict", "reason": "wrong_type"}
        # Expected-CAS token comes from the REQUEST (captured at render), not a
        # fresh read — a foreign status advance since render then fails the
        # predicate inside the adapter's immediate transaction (fail-closed).
        try:
            order = int(payload.get("expected_order"))
        except (TypeError, ValueError):
            return {"ok": False, "outcome": "conflict",
                    "reason": "invalid_status_version"}
        event_id = payload.get("expected_event_id")
        expected_status = payload.get("expected_status")
        if not event_id or not expected_status or order <= 0:
            return {"ok": False, "outcome": "conflict",
                    "reason": "invalid_status_version"}
        token = StatusToken(tid, expected_status, order, event_id)
        if action == "done":
            res = transition_status(self._db_path, token, "done",
                                    actor_id="operator", forbid_path=None)
            target = "done"
        else:
            previous = payload.get("previous_status")
            if previous not in ("not_started", "in_progress"):
                return {"ok": False, "outcome": "conflict",
                        "reason": "bad_undo_target"}
            res = transition_status(self._db_path, token, previous, undo=True,
                                    actor_id="operator", forbid_path=None)
            target = previous
        out = self._jsonable_result(res)
        out["ok"] = res.get("outcome") == "applied"
        if not out["ok"] and res.get("outcome") == "conflict":
            con = self.db.connect()
            try:
                cur = con.execute(
                    "SELECT status FROM tasks WHERE id=?", (tid,)
                ).fetchone()
            finally:
                con.close()
            if cur and cur["status"] == target:
                out = {"ok": True, "outcome": "noop",
                       "reason": "already_" + target, "id": tid}
        out.setdefault("id", tid)
        return out

    @staticmethod
    def _jsonable_result(res: dict) -> dict:
        """Convert StatusToken/UndoToken dataclasses in a result to plain dicts
        so the response serialises to JSON and the client can echo the undo
        token back on the undo request."""
        out = {}
        for k, v in res.items():
            if hasattr(v, "__dataclass_fields__"):
                out[k] = {f: getattr(v, f) for f in v.__dataclass_fields__}
            else:
                out[k] = v
        return out


# --------------------------------------------------------------------------- #
#  HTTP слой
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html>
<html lang="bg">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="board-csrf" content="__BOARD_CSRF__">
<title>Operator Board</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --border:#2a3240;
    --txt:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --accent2:#7ee787;
    --h:#ff7b72; --m:#e3b341; --l:#8b949e; --chip:#21262d;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,"Noto Sans",sans-serif;
       background:var(--bg);color:var(--txt)}
  header{position:sticky;top:0;z-index:10;background:var(--panel);
         border-bottom:1px solid var(--border);padding:10px 16px;
         display:flex;gap:14px;align-items:center;flex-wrap:wrap}
  header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px}
  header .sub{color:var(--muted);font-size:12px}
  .tabs{display:flex;gap:6px;flex-wrap:wrap}
  .tab{background:var(--chip);border:1px solid var(--border);color:var(--txt);
       padding:6px 12px;border-radius:8px;cursor:pointer;font-size:13px}
  .tab.active{background:var(--accent);color:#04121f;border-color:var(--accent);font-weight:600}
  .search{margin-left:auto;display:flex;gap:6px;align-items:center}
  .search input{background:var(--panel2);border:1px solid var(--border);color:var(--txt);
       padding:7px 11px;border-radius:8px;width:260px;font-size:13px}
  main{padding:16px;max-width:1080px;margin:0 auto}
  .view{display:none} .view.active{display:block}
  .grp{margin:14px 0}
  .grp h2{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);
          margin:0 0 8px;display:flex;gap:8px;align-items:center}
  .count{background:var(--chip);border-radius:20px;padding:1px 8px;font-size:11px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;
        padding:10px 12px;margin:7px 0;cursor:pointer;transition:border-color .12s}
  .card:hover{border-color:var(--accent)}
  .card .row1{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
  .card .line{margin-top:3px;color:var(--txt)}
  .card.reply{margin-left:26px;border-left:2px solid var(--accent)}
  .badge{font-size:10.5px;padding:1px 7px;border-radius:6px;background:var(--chip);
         color:var(--muted);white-space:nowrap}
  .role{font-weight:600;color:var(--accent)}
  .kind{color:var(--accent2)}
  .pri-H{color:var(--h);border:1px solid var(--h)}
  .pri-M{color:var(--m);border:1px solid var(--m)}
  .pri-L,.pri-INFO{color:var(--l)}
  .age{color:var(--muted);font-size:11.5px;margin-left:auto}
  .mid{color:var(--muted);font-size:11px;font-family:ui-monospace,monospace}
  .body{display:none;white-space:pre-wrap;margin-top:8px;padding:9px;border-radius:8px;
        background:var(--panel2);color:#c9d1d9;font-size:13px;
        border:1px solid var(--border);max-height:420px;overflow:auto}
  .card.open .body{display:block}
  .waiting-on{color:var(--m);font-size:11.5px}
  .ctrl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
  .ctrl select,.ctrl input{background:var(--panel2);border:1px solid var(--border);
       color:var(--txt);padding:5px 9px;border-radius:7px;font-size:13px}
  .empty{color:var(--muted);padding:20px;text-align:center}
  .tlink{color:var(--accent);cursor:pointer;text-decoration:none}
  .src-tag{font-size:10px;padding:1px 6px;border-radius:5px;background:#1f6feb33;
           color:var(--accent)}
  .knw .src-tag{background:#7ee78733;color:var(--accent2)}
  .tsk .src-tag{background:#e3b34133;color:var(--m)}
  mark{background:#3b2f00;color:#ffd866;padding:0 1px;border-radius:2px}
  .foot{color:var(--muted);font-size:11px;text-align:center;padding:16px}
  /* --- client-side sort/filter controls (no server round-trip) --- */
  .controls{background:var(--panel);border:1px solid var(--border);border-radius:10px;
            padding:8px 10px;margin:8px 0 10px;display:flex;flex-direction:column;gap:7px}
  .ctl-row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
  .ctl-lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px;
           min-width:58px}
  .sortbtn{background:var(--chip);border:1px solid var(--border);color:var(--txt);
           padding:4px 9px;border-radius:7px;cursor:pointer;font-size:12px}
  .sortbtn.on{background:var(--accent);color:#04121f;border-color:var(--accent);font-weight:600}
  .chip{background:var(--chip);border:1px solid var(--border);color:var(--muted);
        padding:3px 9px;border-radius:20px;cursor:pointer;font-size:12px;user-select:none}
  .chip.on{color:#04121f;font-weight:600}
  .chip.on.pc{background:var(--h);border-color:var(--h)}
  .chip.on{background:var(--accent2);border-color:var(--accent2)}
  .txtfilter{background:var(--panel2);border:1px solid var(--border);color:var(--txt);
             padding:5px 10px;border-radius:7px;font-size:12px;flex:1;min-width:160px}
  .rescount{color:var(--muted);font-size:11px;margin-left:auto;white-space:nowrap}
  .stale-tag{background:#5a3a00;color:#ffcf70;font-size:10px;padding:1px 6px;border-radius:5px}
  .fwd-tag{color:var(--accent2);font-size:11px}
  .sec-hd{font-size:13px;font-weight:600;margin:16px 0 2px;display:flex;gap:8px;align-items:center}
  .sec-sub{color:var(--muted);font-size:11px;font-weight:400}
  /* --- select + clipboard (reuse на tray "selectable/copyable" подхода, web-native) --- */
  .cardtools{float:right;display:flex;gap:4px;margin-left:8px}
  .copybtn,.closebtn{cursor:pointer;font-size:12px;line-height:1;padding:2px 7px;border-radius:6px;
        border:1px solid var(--border);background:var(--chip);color:var(--muted);user-select:none}
  .copybtn:hover{color:var(--accent);border-color:var(--accent)}
  .closebtn:hover{color:var(--h);border-color:var(--h)}
  /* --- "Завърши" (status->done) контрол + inline confirm + undo --- */
  .donebtn{cursor:pointer;font-size:12px;line-height:1;padding:2px 8px;border-radius:6px;
        border:1px solid var(--border);background:var(--chip);color:var(--muted);user-select:none}
  .donebtn:hover{color:var(--ok,#2ecc71);border-color:var(--ok,#2ecc71)}
  .donebtn:focus{outline:2px solid var(--accent);outline-offset:1px}
  .confirm{margin-top:8px;padding:8px 10px;border:1px solid var(--border);border-radius:8px;
        background:var(--panel2);font-size:12px;user-select:none}
  .confirm .cq{color:var(--txt);margin-bottom:6px}
  .confirm .cid{color:var(--muted);font-family:monospace;font-size:11px}
  .confirm button{font-size:12px;padding:3px 10px;border-radius:6px;cursor:pointer;
        border:1px solid var(--border);margin-right:6px}
  .confirm .yes{background:transparent;color:var(--ok,#2ecc71);border-color:var(--ok,#2ecc71)}
  .confirm .no{background:var(--accent);color:#04121f;border-color:var(--accent);font-weight:600}
  .undobtn{cursor:pointer;font-size:12px;line-height:1;padding:2px 8px;border-radius:6px;
        border:1px solid var(--accent);background:transparent;color:var(--accent);user-select:none}
  .closed-note{color:var(--muted);font-size:11px;margin-top:6px;font-style:italic}
  .card.archived{opacity:.45}
  .card.done{opacity:.55}
  #toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(64px);
        background:var(--accent);color:#04121f;padding:9px 18px;border-radius:10px;font-weight:600;
        font-size:13px;opacity:0;transition:all .25s;z-index:100;pointer-events:none;max-width:80vw}
  #toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  #toast.err{background:var(--h)}
  /* контроли/бутони НИКОГА не влизат в copy-селекция; съдържанието — да */
  .controls,.cardtools,.tabs,.sortbtn,.chip,.copybtn,.closebtn,.donebtn,.undobtn,.confirm,header .search,.ctl-row .ctl-lbl{user-select:none}
  .card .line,.card .body,.card .mid{user-select:text;cursor:text}
</style>
</head>
<body>
<header>
  <h1>🗂 Operator Board <span class="sub" id="mode">null-floor · read-only · no-LLM</span></h1>
  <div class="tabs">
    <div class="tab active" data-v="waiting">Какво чака мен</div>
    <div class="tab" data-v="recent">Какво реши X преди час</div>
    <div class="tab" data-v="topic">Целият дебат по тема</div>
  </div>
  <div class="search">
    <input id="q" placeholder="🔎 глобално търсене (debate · tasks · notes)…" />
  </div>
</header>
<main>
  <section id="v-waiting" class="view active"></section>
  <section id="v-recent" class="view"></section>
  <section id="v-topic" class="view"></section>
  <section id="v-search" class="view"></section>
  <div class="foot" id="foot"></div>
</main>
<div id="toast"></div>
<script>
const $=(s,r=document)=>r.querySelector(s);
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// escape, после превърни FTS маркерите 〈〉 в <mark> подсветка (без raw-HTML риск)
const hl=s=>esc(s).replace(/〈/g,'<mark>').replace(/〉/g,'</mark>');
const j=async u=>(await fetch(u)).json();
let curView="waiting";

// ---- Copy / close машинария (iter3): дефинирано ВЕДНЪЖ, преизползвано навсякъде ----
function toast(msg,isErr){ const t=$('#toast'); t.textContent=msg; t.className='show'+(isErr?' err':'');
  clearTimeout(t._h); t._h=setTimeout(()=>t.className='',1700); }
async function copyText(txt){ if(!txt)return;
  try{ await navigator.clipboard.writeText(txt); }
  catch(e){ const ta=document.createElement('textarea'); ta.value=txt; ta.style.position='fixed';
    ta.style.opacity='0'; document.body.appendChild(ta); ta.select();
    try{document.execCommand('copy');}catch(_){} document.body.removeChild(ta); }
  toast('копирано ✓'); }
// има ли активна НЕПРАЗНА ръчна селекция? (тогава кликът НЕ бива да я хайджаква)
function hasSelection(){ return !!(window.getSelection && String(window.getSelection()).trim()); }
// data-copy → точен текст; иначе fallback към .body на картата (debate без data-copy).
// force=true (⧉ бутонът) → копирай цялото парче ВЪПРЕКИ селекция; иначе, ако
// операторът е маркирал част ръчно → bail-out, за да остане native selection/Ctrl+C.
function copyEl(el, force){ if(!el)return;
  if(!force && hasSelection()) return;
  const t=el.getAttribute('data-copy');
  if(t!=null){ copyText(decodeURIComponent(t)); return; }
  const b=el.querySelector('.body'); if(b) copyText(b.textContent); }
// клик по карта = toggle на тялото, но НЕ докато има ръчна селекция: иначе картата
// колабира, съдържанието „подскача" нагоре и маркираното се губи. Нула re-render,
// нула scroll reset — само локален class toggle върху самата карта.
function cardClick(el){ if(hasSelection()) return; el.classList.toggle('open'); }
function copyAttr(txt){ return `data-copy="${encodeURIComponent(txt||'')}"`; }
// cardtools: САМО copy (⧉). Старият ✕ close (/api/close) е премахнат — /api/close
// остава 405 sentinel. Завършването е ОТДЕЛЕН контрол само за Section-B task/note
// карти (taskTools/doneCtl), който носи CAS-токена.
function cardtools(){
  return `<span class="cardtools"><span class="copybtn" title="копирай" onclick="event.stopPropagation();copyEl(this.closest('.card'),true)">⧉</span></span>`; }
// CSRF nonce — per-process, инжектиран в <meta>; задължителен на всеки write POST.
const CSRF=(document.querySelector('meta[name="board-csrf"]')||{}).content||'';
async function apiTaskStatus(payload){
  try{ const r=await fetch('/api/task-status',{method:'POST',
      headers:{'Content-Type':'application/json','X-Board-CSRF':CSRF},
      body:JSON.stringify(payload)});
    return await r.json(); }
  catch(e){ return {ok:false,outcome:'network',reason:String(e)}; } }
// Section-B tools: copy + "Завърши" (само ако картата носи валиден version токен).
function taskTools(t){
  const cp=`<span class="copybtn" title="копирай" onclick="event.stopPropagation();copyEl(this.closest('.card'),true)">⧉</span>`;
  return `<span class="cardtools">${cp}${doneCtl(t)}</span>`; }
function doneCtl(t){
  if(!(t&&t.id&&t.status_order&&t.status_event_id)) return ''; // fail-closed UX
  const a=`data-id="${esc(t.id)}" data-type="${esc(t.type||'task')}" data-status="${esc(t.status||'')}" data-order="${esc(String(t.status_order))}" data-event="${esc(t.status_event_id)}" data-title="${esc(t.title||'')}"`;
  return `<span class="donebtn" title="завърши (status→done · с undo)" ${a} tabindex="0" role="button" onclick="event.stopPropagation();askDone(this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();event.stopPropagation();askDone(this);}">✓ Завърши</span>`; }
// Inline confirm — default = Отказ (фокусът пада върху "Отказ"); показва title+id+статус.
function askDone(btn){
  const c=btn.closest('.card'); if(!c||c.querySelector('.confirm')) return;
  const id=btn.dataset.id, title=btn.dataset.title||'(без заглавие)', status=btn.dataset.status||'';
  const box=document.createElement('div'); box.className='confirm';
  box.innerHTML=`<div class="cq">Завършване на: <b>${esc(title)}</b></div>`
    +`<div class="cid">id: ${esc(id)} · ${esc(status)} → done</div>`
    +`<div style="margin-top:7px"><button class="no" onclick="event.stopPropagation();this.closest('.confirm').remove()">Отказ</button>`
    +`<button class="yes" onclick="event.stopPropagation();confirmDone(this)">Завърши</button></div>`;
  box.addEventListener('click',e=>e.stopPropagation());
  box._srcBtn=btn; c.appendChild(box);
  const no=box.querySelector('.no'); if(no) no.focus(); }
async function confirmDone(yesBtn){
  const box=yesBtn.closest('.confirm'); const c=box.closest('.card'); const btn=box._srcBtn;
  yesBtn.textContent='…'; yesBtn.disabled=true;
  const d=await apiTaskStatus({id:btn.dataset.id, action:'done', expected_status:btn.dataset.status,
    expected_order:parseInt(btn.dataset.order,10), expected_event_id:btn.dataset.event});
  box.remove();
  if(d.ok&&d.outcome==='applied'){ markDone(c, d.undo_token); toast('✓ завършена · може undo'); }
  else if(d.ok&&d.outcome==='noop'){ markDone(c, null); toast('вече завършена'); }
  else if(d.outcome==='writes_disabled'){ toast('write режимът е изключен (--enable-writes)',true); }
  else { toast('неуспех: '+(d.reason||d.outcome||'?'),true); } }
function markDone(c, undoTok){
  if(!c) return; c.classList.add('done');
  const dc=c.querySelector('.donebtn'); if(dc) dc.remove();
  let note='<div class="closed-note">завършена (status→done)';
  if(undoTok){
    const u=`data-id="${esc(undoTok.task_id)}" data-prev="${esc(undoTok.previous_status)}" data-status="${esc(undoTok.expected_status)}" data-order="${esc(String(undoTok.expected_order))}" data-event="${esc(undoTok.expected_event_id)}"`;
    note+=` · <span class="undobtn" ${u} tabindex="0" role="button" onclick="event.stopPropagation();doUndo(this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();event.stopPropagation();doUndo(this);}">↶ undo</span>`; }
  note+='</div>'; c.insertAdjacentHTML('beforeend', note); }
async function doUndo(btn){
  btn.textContent='…';
  const d=await apiTaskStatus({id:btn.dataset.id, action:'undo', previous_status:btn.dataset.prev,
    expected_status:btn.dataset.status, expected_order:parseInt(btn.dataset.order,10),
    expected_event_id:btn.dataset.event});
  const c=btn.closest('.card');
  if(d.ok){ if(c){ c.classList.remove('done'); const n=c.querySelector('.closed-note'); if(n) n.remove(); }
    toast('↩ върнато (status→'+esc(btn.dataset.prev)+')'); }
  else { toast('undo неуспех: '+(d.reason||d.outcome||'?'),true); btn.textContent='↶ undo'; } }

function showView(v){
  curView=v;
  document.querySelectorAll('.view').forEach(e=>e.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('active',e.dataset.v===v));
  $('#v-'+v).classList.add('active');
  if(v==='waiting')loadWaiting();
  if(v==='recent')loadRecent();
  if(v==='topic')loadTopics();
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>showView(t.dataset.v));

function card(m,cls=''){
  const pri=m.priority?`<span class="badge pri-${m.priority}">${m.priority}</span>`:'';
  const reply=m.reply_to?`<span class="badge">↳ отговор на ${esc(m.reply_to.slice(0,8))}</span>`:'';
  return `<div class="card ${cls}" onclick="cardClick(this)">
    ${cardtools('')}
    <div class="row1">
      <span class="role">${esc(m.role||'')}</span>
      <span class="badge kind">${esc(m.kind||'')}</span>${pri}${reply}
      <span class="mid">${esc((m.msg_id||'').slice(0,8))}</span>
      <span class="age">${esc(m.age||'')}</span>
    </div>
    <div class="line">${esc(m.line||m.snippet||'')}</div>
    <div class="body">${esc(m.body||'')}</div>
  </div>`;
}

// ---- Клиентски sort/filter/text контролер (нула server round-trip, no-LLM) ----
const PRI_ORDER={critical:0,high:1,medium:2,low:3,H:0,M:1,L:2,INFO:3,'':9};
const CTRL={}; // персистентно състояние по ключ (пази се при смяна на view)
function dueBuckets(due){
  if(!due)return['none'];
  const d=new Date(due+'T00:00:00'); if(isNaN(d))return['none'];
  const n=new Date(); n.setHours(0,0,0,0);
  const diff=Math.round((d-n)/86400000), b=[];
  if(diff<0)b.push('overdue');
  if(diff>=0&&diff<=7)b.push('le7');
  if(diff>=0&&diff<=21)b.push('le21');
  return b.length?b:['future'];
}
const CMP={
  priority:(a,b,k)=>(PRI_ORDER[a[k]]??99)-(PRI_ORDER[b[k]]??99),
  text:(a,b,k)=>String(a[k]||'').localeCompare(String(b[k]||''),'bg'),
  date:(a,b,k)=>{const x=a[k]||'~~', y=b[k]||'~~'; return x<y?-1:x>y?1:0;}, // празни най-долу (asc)
  ts:(a,b,k)=>{const x=a[k]||'', y=b[k]||''; return x<y?-1:x>y?1:0;},
};
function controlled(host, key, rows, cfg){
  if(!rows||!rows.length){ host.innerHTML=`<div class="empty">${esc(cfg.empty||'няма елементи')}</div>`; return; }
  const st = CTRL[key] || (CTRL[key]={sort:cfg.defaultSort?{...cfg.defaultSort}:{k:null,dir:1}, f:{}, q:''});
  const opts={};
  for(const fl of (cfg.filters||[])){
    if(fl.kind==='due'){ opts[fl.key]=[['overdue','Просрочени'],['le7','≤7д'],['le21','≤21д'],['none','Без срок']]; }
    else if(fl.options){ opts[fl.key]=fl.options; }
    else { const s=[...new Set(rows.map(r=>r[fl.key]).filter(Boolean))]
             .sort((a,b)=>(PRI_ORDER[a]??99)-(PRI_ORDER[b]??99)||String(a).localeCompare(String(b),'bg'));
           opts[fl.key]=s.map(v=>[v,v]); }
  }
  function passes(r){
    for(const fl of (cfg.filters||[])){
      const sel=st.f[fl.key]; if(!sel||!sel.size)continue;
      if(fl.kind==='due'){ if(!dueBuckets(r[fl.key]).some(x=>sel.has(x)))return false; }
      else if(!sel.has(r[fl.key]))return false;
    }
    if(st.q){ const hay=(cfg.text?cfg.text(r):Object.values(r).join(' ')).toLowerCase();
              if(!hay.includes(st.q.toLowerCase()))return false; }
    return true;
  }
  function apply(){
    let vis=rows.filter(passes);
    if(st.sort.k){ const s=cfg.sorts.find(x=>x.k===st.sort.k); const cmp=CMP[s.type];
      vis=vis.slice().sort((a,b)=>cmp(a,b,st.sort.k)*st.sort.dir); }
    $('#'+key+'-body').innerHTML=vis.map(cfg.row).join('')||`<div class="empty">няма съвпадения при тези филтри</div>`;
    $('#'+key+'-cnt').textContent=`${vis.length} / ${rows.length}`;
  }
  let h=`<div class="controls">`;
  if(cfg.sorts&&cfg.sorts.length){ h+=`<div class="ctl-row"><span class="ctl-lbl">Сорт</span>`+
    cfg.sorts.map(s=>`<span class="sortbtn ${st.sort.k===s.k?'on':''}" data-sk="${s.k}">${esc(s.label)}${st.sort.k===s.k?(st.sort.dir>0?' ▲':' ▼'):''}</span>`).join('')+`</div>`; }
  for(const fl of (cfg.filters||[])){
    h+=`<div class="ctl-row"><span class="ctl-lbl">${esc(fl.label)}</span>`+
      opts[fl.key].map(([v,lbl])=>`<span class="chip ${(st.f[fl.key]&&st.f[fl.key].has(v))?'on':''} ${fl.pc?'pc':''}" data-fk="${esc(fl.key)}" data-fv="${esc(v)}">${esc(lbl)}</span>`).join('')+`</div>`;
  }
  h+=`<div class="ctl-row"><input class="txtfilter" id="${key}-q" placeholder="🔍 филтър в рамките…" value="${esc(st.q)}"><span class="rescount" id="${key}-cnt"></span></div>`;
  h+=`</div><div id="${key}-body"></div>`;
  host.innerHTML=h;
  host.querySelectorAll('[data-sk]').forEach(el=>el.onclick=()=>{
    const k=el.dataset.sk;
    if(st.sort.k===k)st.sort.dir*=-1; else{ st.sort.k=k; st.sort.dir=(cfg.sorts.find(x=>x.k===k).dir)||1; }
    controlled(host,key,rows,cfg); // rebuild → освежи ▲/▼
  });
  host.querySelectorAll('[data-fk]').forEach(el=>el.onclick=()=>{
    const k=el.dataset.fk,v=el.dataset.fv; st.f[k]=st.f[k]||new Set();
    st.f[k].has(v)?st.f[k].delete(v):st.f[k].add(v);
    el.classList.toggle('on'); apply();
  });
  const qi=$('#'+key+'-q'); qi.oninput=()=>{ st.q=qi.value; apply(); };
  apply();
}
const PMAP={critical:'H',high:'H',medium:'M',low:'L'};
function taskRow(t){
  const over=t.due_date&&dueBuckets(t.due_date).includes('overdue');
  const due=t.due_date
    ?`<span class="badge" style="${over?'color:var(--h);border-color:var(--h)':''}">📅 ${esc(t.due_date)}${over?' · просрочена':''}</span>`
    :`<span class="badge">без срок</span>`;
  const copy=`${t.title}\n${t.section||'—'} · ${t.priority||'—'} · срок ${t.due_date||'—'} · ${t.project||'—'} · ${t.status||''} · ${t.id}`;
  return `<div class="card" ${copyAttr(copy)} onclick="cardClick(this)">
    ${taskTools(t)}
    <div class="row1"><span class="badge">${esc(t.section)}</span>
    ${t.priority?`<span class="badge pri-${PMAP[t.priority]||'INFO'}">${esc(t.priority)}</span>`:''}
    ${t.project?`<span class="badge">${esc(t.project)}</span>`:''}
    ${due}<span class="age">${esc(t.age)}</span></div>
    <div class="line">${esc(t.title)}</div></div>`;
}
function aRow(m){
  return `<div class="card" onclick="cardClick(this)">
    ${cardtools('')}
    <div class="row1"><span class="role">${esc(m.role)}</span>
    <span class="badge kind">${esc(m.kind)}</span>
    ${m.priority?`<span class="badge pri-${m.priority}">${esc(m.priority)}</span>`:''}
    ${m.stale?`<span class="stale-tag">⚠ остарял</span>`:''}
    <span class="mid">${esc((m.msg_id||'').slice(0,8))}</span>
    <span class="age">${esc(m.age)}</span></div>
    <div class="line">${esc(m.line)}</div>
    <div class="fwd-tag">↦ чака: ${esc(m.fwd)}</div>
    <div class="body">${esc(m.body)}</div></div>`;
}

async function loadWaiting(){
  const d=await j('/api/waiting');
  const el=$('#v-waiting');
  el.innerHTML=`
    <div class="sec-hd">A · Решения, чакащи ТЕБ
      <span class="sec-sub">${d.section_a_count} от ${d.section_a_before} · само генуинни operator Q/DECISION заявки (inter-agent шумът филтриран)</span></div>
    <div id="secA"></div>
    <div class="sec-hd">B · Твои задачи (сега)
      <span class="sec-sub">${d.section_b_count} от ${d.section_b_before} отворени today/next · релевантен времеви прозорец (≤21д / просрочени / без-срок high)</span></div>
    <div id="secB"></div>`;
  controlled($('#secA'),'secA', d.section_a, {
    empty:'Нищо не чака теб в дебата — чисто ✔',
    defaultSort:{k:'ts',dir:-1},
    sorts:[{k:'ts',label:'възраст',type:'ts',dir:-1},{k:'priority',label:'приоритет',type:'priority',dir:1},{k:'role',label:'роля',type:'text',dir:1},{k:'kind',label:'вид',type:'text',dir:1}],
    filters:[{key:'priority',label:'приоритет',options:[['H','H'],['M','M'],['L','L'],['INFO','INFO']],pc:1},{key:'role',label:'роля'},{key:'kind',label:'вид'}],
    text:m=>`${m.role} ${m.kind} ${m.line} ${m.body} ${m.fwd}`,
    row:aRow,
  });
  controlled($('#secB'),'secB', d.section_b, {
    empty:'Няма задачи в прозореца',
    defaultSort:{k:'due_date',dir:1},
    sorts:[{k:'due_date',label:'срок',type:'date',dir:1},{k:'priority',label:'приоритет',type:'priority',dir:1},{k:'project',label:'проект',type:'text',dir:1},{k:'section',label:'секция',type:'text',dir:1},{k:'updated_at',label:'ъпдейт',type:'ts',dir:-1}],
    filters:[{key:'priority',label:'приоритет',options:[['critical','critical'],['high','high'],['medium','medium'],['low','low']],pc:1},{key:'project',label:'проект'},{key:'section',label:'секция',options:[['today','today'],['next','next']]},{key:'due_date',label:'срок',kind:'due'}],
    text:t=>`${t.title} ${t.project} ${t.section} ${t.priority}`,
    row:taskRow,
  });
  $('#foot').textContent='Генерирано '+d.generated_at;
}

async function loadRecent(){
  const el=$('#v-recent');
  if(!el.dataset.init){
    el.innerHTML=`<div class="ctrl">
      Прозорец (сървър): <select id="rh">
        <option value="1">1 час</option><option value="3">3 часа</option>
        <option value="6">6 часа</option><option value="12">12 часа</option>
        <option value="24" selected>24 часа</option><option value="72">3 дни</option>
        <option value="168">7 дни</option></select>
      Извличай: <select id="rk">
        <option value="DECISION,STATE,STATUS" selected>решения + статуси</option>
        <option value="DECISION,STATE,STATUS,A,Q,PING">всичко значещо</option>
        <option value="DECISION">само DECISION</option>
        <option value="DECISION,STATE">DECISION + STATE</option>
        <option value="STATUS">само STATUS</option></select>
    </div><div id="recent-list"></div>`;
    el.dataset.init="1";
    $('#rh').onchange=$('#rk').onchange=fetchRecent;
  }
  fetchRecent();
}
async function fetchRecent(){
  const h=$('#rh').value,k=$('#rk').value;
  const d=await j(`/api/recent?hours=${h}&kinds=${encodeURIComponent(k)}`);
  controlled($('#recent-list'),'recent', d.items, {
    empty:'Нищо в този прозорец',
    defaultSort:{k:'ts',dir:-1},
    sorts:[{k:'ts',label:'възраст',type:'ts',dir:-1},{k:'priority',label:'приоритет',type:'priority',dir:1},{k:'role',label:'роля',type:'text',dir:1},{k:'kind',label:'вид',type:'text',dir:1}],
    filters:[{key:'priority',label:'приоритет',options:[['H','H'],['M','M'],['L','L'],['INFO','INFO']],pc:1},{key:'role',label:'роля'},{key:'kind',label:'вид'}],
    text:m=>`${m.role} ${m.kind} ${m.line} ${m.body}`,
    row:m=>card(m),
  });
  $('#foot').textContent=`${d.count} съобщения · последни ${d.hours} ч · клиентски филтри отгоре`;
}

async function loadTopics(){
  const el=$('#v-topic');
  const d=await j('/api/topics');
  let h=`<div class="ctrl"><input id="tf" placeholder="филтър по тема…" style="width:340px"/>
    <span class="count">${d.count} теми</span></div><div id="tlist"></div>`;
  el.innerHTML=h;
  const render=(flt)=>{
    const arr=d.topics.filter(t=>!flt||(t.title+t.topic_id).toLowerCase().includes(flt.toLowerCase()));
    $('#tlist').innerHTML=arr.map(t=>`<div class="card" onclick="openTopic('${t.topic_id}')">
      <div class="row1"><span class="role">${esc(t.title)}</span>
      ${t.state?`<span class="badge">${esc(t.state)}</span>`:''}
      <span class="badge">${t.count} съобщ.</span>
      <span class="age">${esc(t.age||'')}</span></div>
      <div class="mid">${esc(t.topic_id)}</div></div>`).join('')||`<div class="empty">Няма теми</div>`;
  };
  render('');
  $('#tf').oninput=e=>render(e.target.value);
}
async function openTopic(tid){
  const d=await j('/api/topic?id='+encodeURIComponent(tid));
  const el=$('#v-topic');
  let h=`<div class="ctrl"><span class="tlink" onclick="loadTopics()">← теми</span>
    <b style="margin-left:8px">${esc(d.title)}</b>
    ${d.state?`<span class="badge">${esc(d.state)}</span>`:''}
    <span class="count">${d.count} съобщ. (без WATERMARK)</span></div>`;
  h+=d.messages.map(m=>card(m,m.reply_to?'reply':'')).join('')||`<div class="empty">Празна тема</div>`;
  el.innerHTML=h;
  $('#foot').textContent=d.topic_id;
}

let stimer=null;
$('#q').addEventListener('input',e=>{
  clearTimeout(stimer);
  const v=e.target.value.trim();
  stimer=setTimeout(()=>{ v.length>=2?runSearch(v):showView(curView==='search'?'waiting':curView); },220);
});
async function runSearch(q){
  document.querySelectorAll('.view').forEach(e=>e.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(e=>e.classList.remove('active'));
  $('#v-search').classList.add('active');
  const d=await j('/api/search?q='+encodeURIComponent(q)+'&limit=25');
  let h='';
  h+=grp('Дебат',d.debate.map(m=>`<div class="card" onclick="cardClick(this)">
      ${cardtools('')}
      <div class="row1"><span class="src-tag">debate</span><span class="role">${esc(m.role)}</span>
      <span class="badge kind">${esc(m.kind)}</span><span class="mid">${esc((m.msg_id||'').slice(0,8))}</span>
      <span class="age">${esc(m.age||'')}</span></div>
      <div class="line">${hl(m.snippet||'')}</div><div class="body">${esc(m.body)}</div></div>`).join(''),d.debate.length);
  h+=`<div class="tsk">`+grp('Задачи и ноти',d.tasks.map(t=>`<div class="card" ${copyAttr(`${t.title}\n${t.type||'task'} · ${t.section||'—'} · ${t.project||'—'} · ${t.status||''} · ${t.id}`)}>
      ${cardtools()}
      <div class="row1"><span class="src-tag">${esc(t.type||'task')}</span>
      ${t.section?`<span class="badge">${esc(t.section)}</span>`:''}
      ${t.project?`<span class="badge">${esc(t.project)}</span>`:''}
      <span class="age">${esc(t.status||'')}</span></div>
      <div class="line">${esc(t.title)}</div>
      <div class="mid">${hl(t.snippet||'')}</div></div>`).join(''),d.tasks.length)+`</div>`;
  h+=`<div class="knw">`+grp('Знание (граф)',d.knowledge.map(k=>`<div class="card">
      <div class="row1"><span class="src-tag">${esc(k.type||'entity')}</span>
      <span class="role">${esc(k.name)}</span>
      ${k.project?`<span class="badge">${esc(k.project)}</span>`:''}</div>
      <div class="mid">${hl(k.snippet||'')}</div></div>`).join(''),d.knowledge.length)+`</div>`;
  $('#v-search').innerHTML=h;
  $('#foot').textContent=`търсене: "${q}" · ${d.debate.length+d.tasks.length+d.knowledge.length} резултата`;
}
function grp(title,inner,n){
  return `<div class="grp"><h2>${title} <span class="count">${n}</span></h2>`+
    (inner||`<div class="empty">няма съвпадения</div>`)+`</div>`;
}
showView('waiting');
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    board: Board = None  # инжектира се
    csrf: str = ""       # per-process CSRF nonce (инжектира се в main)

    def log_message(self, *a):  # тихо
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(
            code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _client_is_loopback(self) -> bool:
        host = (self.client_address or ("",))[0]
        return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                # Inject the per-process CSRF nonce into the served HTML only.
                page = PAGE.replace("__BOARD_CSRF__", html.escape(self.csrf or ""))
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/waiting":
                self._json(self.board.waiting())
            elif u.path == "/api/recent":
                hours = float(qs.get("hours", ["6"])[0])
                role = (qs.get("role", [""])[0] or "").strip() or None
                kinds = (qs.get("kinds", ["DECISION,STATE,STATUS"])[0]).split(",")
                self._json(self.board.recent(hours, role, kinds))
            elif u.path == "/api/topics":
                self._json(self.board.topics())
            elif u.path == "/api/topic":
                tid = qs.get("id", [""])[0]
                self._json(self.board.topic_thread(tid))
            elif u.path == "/api/search":
                q = qs.get("q", [""])[0]
                limit = int(qs.get("limit", ["25"])[0])
                self._json(self.board.search(q, min(limit, 100)))
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:  # никога не сваляй сървъра заради 1 заявка
            self._json({"error": str(e), "path": u.path})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/task-status":
                # Narrow status-write surface. Layered fail-closed boundary:
                # loopback-only -> CSRF (constant-time) -> exact JSON content
                # type -> bounded body -> CAS transition via the audited
                # adapter. NEVER touches debate_messages; NO subprocess.
                if not self._client_is_loopback():
                    self._json({"ok": False, "outcome": "forbidden",
                                "reason": "non_loopback"}, 403)
                    return
                sent = self.headers.get("X-Board-CSRF", "") or ""
                if not self.csrf or not hmac.compare_digest(sent, self.csrf):
                    self._json({"ok": False, "outcome": "forbidden",
                                "reason": "csrf"}, 403)
                    return
                ctype = (self.headers.get("Content-Type", "") or "").split(
                    ";")[0].strip().lower()
                if ctype != "application/json":
                    self._json({"ok": False, "outcome": "unsupported_media",
                                "reason": "content_type"}, 415)
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > 4096:
                    self._json({"ok": False, "outcome": "too_large",
                                "reason": "body_size"}, 413)
                    return
                raw = self.rfile.read(n) if n else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                    if not isinstance(payload, dict):
                        raise ValueError("not an object")
                except Exception:
                    self._json({"ok": False, "outcome": "bad_request",
                                "reason": "malformed_json"}, 400)
                    return
                res = self.board.task_status(payload)
                code = {"applied": 200, "noop": 200, "conflict": 409,
                        "writes_disabled": 403, "unavailable": 503}.get(
                    res.get("outcome"), 400)
                self._json(res, code)
            elif u.path == "/api/close":
                # CONTAINMENT (дебат DAILY_20260704, операторски GO 013b92958e5e
                # след ADVOCATE_CODEX HOLD acb9b91c901f): /api/close е ИЗКЛЮЧЕН.
                # Нула helper subprocess, нула DB/JSONL write — само 405 sentinel.
                n = int(self.headers.get("Content-Length", "0") or 0)
                if n:
                    self.rfile.read(n)
                body = json.dumps(
                    {"ok": False, "note": "close_disabled_containment",
                     "sentinel": "CONTAINMENT-20260718-APICLOSE-DISABLED-013b92958e5e"},
                    ensure_ascii=False).encode("utf-8")
                self._send(405, body, "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:
            self._json({"ok": False, "error": str(e)})


def main():
    ap = argparse.ArgumentParser(description="Operator Board (null-floor, read-only)")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"път до memory.db (по подр. {DEFAULT_DB})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--no-open", action="store_true", help="не отваряй браузър")
    ap.add_argument("--enable-writes", action="store_true",
                    help="разреши тесния статус-write (POST /api/task-status, "
                         "CAS status->done за task/note); по подразбиране OFF "
                         "и бордът е строго read-only")
    args = ap.parse_args()

    db = DB(args.db)
    Handler.board = Board(db, enable_writes=args.enable_writes)
    # Per-process CSRF nonce — regenerated at every launch; required on every
    # /api/task-status POST as the X-Board-CSRF header.
    Handler.csrf = secrets.token_urlsafe(32)

    caps = db.caps
    print("=" * 60)
    print(" OPERATOR BOARD — null-floor · no-LLM"
          + ("  · WRITES: task-status ON" if args.enable_writes
             else "  · read-only"))
    print("=" * 60)
    print(f" DB (mode=ro reads): {args.db}")
    print(f" status-writes: {'ENABLED (CAS status->done)' if args.enable_writes else 'disabled'}")
    print(f" debate={caps['has_debate']} tasks={caps['has_tasks']} "
          f"tasks_fts={caps['has_tasks_fts']} memory_fts={caps['has_memory_fts']} "
          f"recipients={caps['has_recipients']}")
    url = f"http://{args.host}:{args.port}"
    print(f" → {url}")
    print("=" * 60)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[stop] спрян от оператора")
        srv.shutdown()


if __name__ == "__main__":
    main()
