#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""close_task.py — РЕВЕРСИБЛЕ затваряне (archive) на ЕДНА задача по id.

Използва ТОЧНО официалния safe-write път на sqlite-memory-mcp:
    db_utils.get_conn_immediate()  (production write wrapper, BEGIN IMMEDIATE,
                                    busy-retry, WAL) +
    db_utils.apply_task_mutation() (валидация на колони + task_field_versions
                                    ledger + HLC/logical-clock + provenance
                                    events + bridge-safe tombstone reset)

Това е СЪЩИЯТ път, който auto_archive.py ползва за archive. НУЛА raw SQL UPDATE.
Единствената позволена мутация тук е status -> 'archived' (реверсибле, НЕ delete).

Usage:
    python3 close_task.py --id <task_id> [--db PATH] [--dry-run] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

# db_utils е в root-а на repo-то (един над operator_board/).
_ROOT = os.environ.get("SMEM_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db_utils import (  # noqa: E402
    DB_PATH,
    apply_task_mutation,
    get_conn_immediate,
    now_iso,
)

_TARGET_STATUS = "archived"
_TOOL_NAME = "operator_board.close"


def _peek(db_path: str, task_id: str) -> dict | None:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT id, title, status, section, type, project FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def close_task(task_id: str, db_path: str | None, dry_run: bool) -> dict:
    target_db = db_path or DB_PATH
    cur = _peek(target_db, task_id)
    if cur is None:
        return {"id": task_id, "ok": False, "updated": 0, "note": "not_found"}
    old_status = cur["status"]
    base = {"id": task_id, "title": cur["title"], "old_status": old_status,
            "new_status": _TARGET_STATUS}
    if old_status == _TARGET_STATUS:
        return {**base, "ok": True, "updated": 0, "note": "already_archived"}
    if dry_run:
        return {**base, "ok": True, "updated": 0, "note": "dry_run"}
    with get_conn_immediate(db_path) as conn:
        result = apply_task_mutation(
            conn, task_id, {"status": _TARGET_STATUS},
            timestamp=now_iso(), tool_name=_TOOL_NAME,
            actor_type="human", actor_id="operator", source_kind="task",
        )
    if result.get("missing"):
        return {**base, "ok": False, "updated": 0, "note": "not_found"}
    return {**base, "ok": True, "updated": int(result.get("updated", 0)),
            "note": "archived" if result.get("updated") else "noop"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--db", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = close_task(a.id, a.db, a.dry_run)
    print(json.dumps(res, ensure_ascii=False) if a.json else res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
