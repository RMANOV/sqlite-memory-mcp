"""Version-guarded task status transitions for native clients.

The module is deliberately not wired into the live tray. It owns the
``BEGIN IMMEDIATE`` transaction used by the optional native close surface and
delegates the conditional update and ledger write to ``apply_task_mutation``.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from db_utils import (
    DB_PATH,
    apply_task_mutation,
    get_conn_immediate,
    get_status_version,
)

_ACTIVE = frozenset({"not_started", "in_progress"})
_TERMINAL = frozenset({"archived", "cancelled"})


@dataclass(frozen=True)
class StatusToken:
    task_id: str
    status: str
    updated_order: int
    source_event_id: str


@dataclass(frozen=True)
class UndoToken:
    task_id: str
    previous_status: str
    expected_status: str
    expected_order: int
    expected_event_id: str


def status_token(conn: sqlite3.Connection, task_id: str) -> StatusToken | None:
    """Read the rendered status and its logical version as one token."""
    row = conn.execute(
        "SELECT id, type, status FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    version = get_status_version(conn, task_id)
    if (
        row is None
        or row[1] not in ("task", "note")
        or version is None
        or version[0] == 0
        or not isinstance(version[1], str)
        or not version[1].strip()
    ):
        return None
    return StatusToken(task_id, row[2], version[0], version[1])


def _allowed(source: str, target: str, *, confirmed: bool, undo: bool) -> bool:
    if undo:
        return source == "archived" and target in _ACTIVE | {"done"}
    if source in _ACTIVE and target == "done":
        return True
    if source == "done" and target == "archived":
        return True
    if source in _ACTIVE and target == "archived":
        return confirmed
    return False


def transition_status(
    db_path: str,
    token: StatusToken,
    target: str,
    *,
    confirmed: bool = False,
    undo: bool = False,
    actor_id: str = "operator",
    forbid_path: str | None = DB_PATH,
) -> dict[str, Any]:
    """Apply one guarded transition and return ``applied|noop|conflict``.

    The function accepts only the frozen status transition matrix. A request
    that reaches this function with an advanced token is a foreign conflict,
    even when the current value already equals ``target``.
    """
    if forbid_path is not None and os.path.realpath(db_path) == os.path.realpath(
        forbid_path
    ):
        raise PermissionError(f"status transition refused fenced DB path: {db_path}")
    if token.updated_order <= 0 or not token.source_event_id:
        return {
            "outcome": "conflict",
            "updated": 0,
            "reason": "invalid_status_version",
        }
    if token.status in _TERMINAL and not undo:
        return {"outcome": "conflict", "updated": 0, "reason": "terminal"}
    if not _allowed(token.status, target, confirmed=confirmed, undo=undo):
        return {"outcome": "conflict", "updated": 0, "reason": "transition"}

    try:
        with get_conn_immediate(db_path) as conn:
            result = apply_task_mutation(
                conn,
                token.task_id,
                {"status": target},
                actor_type="human",
                actor_id=actor_id,
                tool_name="task_status_cas.transition_status",
                source_kind="task",
                source_ref=token.task_id,
                expected_status=token.status,
                expected_status_order=token.updated_order,
                expected_status_event_id=token.source_event_id,
            )
            if result.get("outcome") != "applied":
                return result
            version = get_status_version(conn, token.task_id)
            if version is None or version[0] == 0 or version[1] is None:
                raise RuntimeError(
                    "applied status transition produced no version token"
                )
            result["status_token"] = StatusToken(
                token.task_id, target, version[0], version[1]
            )
            if target == "archived" and not undo:
                result["undo_token"] = UndoToken(
                    task_id=token.task_id,
                    previous_status=token.status,
                    expected_status=target,
                    expected_order=version[0],
                    expected_event_id=version[1],
                )
            return result
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "busy" in text or "locked" in text:
            return {"outcome": "conflict", "updated": 0, "reason": "busy"}
        raise


class SingleUseUndo:
    """In-memory, single-use wrapper around an archive undo token."""

    def __init__(self, token: UndoToken) -> None:
        self.token = token
        self.consumed = False

    def apply(
        self,
        db_path: str,
        *,
        actor_id: str = "operator",
        forbid_path: str | None = DB_PATH,
    ) -> dict[str, Any]:
        if self.consumed:
            return {"outcome": "noop", "updated": 0, "reason": "consumed"}
        self.consumed = True
        current = StatusToken(
            self.token.task_id,
            self.token.expected_status,
            self.token.expected_order,
            self.token.expected_event_id,
        )
        return transition_status(
            db_path,
            current,
            self.token.previous_status,
            undo=True,
            actor_id=actor_id,
            forbid_path=forbid_path,
        )


class StatusSingleFlight:
    """Absorb repeated UI gestures until the caller reloads the task row."""

    def __init__(self) -> None:
        self._blocked: dict[str, dict[str, Any] | None] = {}

    def begin(self, task_id: str) -> bool:
        if task_id in self._blocked:
            return False
        self._blocked[task_id] = None
        return True

    def finish(self, task_id: str, result: dict[str, Any]) -> None:
        if task_id in self._blocked:
            self._blocked[task_id] = dict(result)

    def replay_result(self, task_id: str) -> dict[str, Any]:
        cached = self._blocked.get(task_id)
        return {
            "outcome": "noop",
            "updated": 0,
            "reason": "single_flight",
            "first_outcome": (cached or {}).get("outcome"),
        }

    def reloaded(self, task_id: str) -> None:
        self._blocked.pop(task_id, None)
