#!/usr/bin/env python3
"""Human-facing debate agent wake event sink.

This module is deliberately file/desktop-notification only. It must not call
debate MCP tools or write debate messages, otherwise wake receipts can recurse.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVENT_PATH = Path(
    os.environ.get(
        "DEBATE_AGENT_EVENT_LOG",
        os.path.expanduser("~/.claude/memory/debate_agent_events.jsonl"),
    )
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _brief_text(text: str, limit: int = 120) -> str:
    normalized = str(text or "").replace("\n", " ").replace("\t", " ")
    words = " ".join(normalized.split())
    if len(words) <= limit:
        return words
    return words[: max(0, limit - 3)].rstrip() + "..."


def append_event(event: dict[str, Any]) -> None:
    try:
        EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": _now(), **event}
        with EVENT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def notify_receipt(event: dict[str, Any]) -> None:
    if not os.environ.get("DISPLAY"):
        return
    sender = str(event.get("from_role") or "UNKNOWN")
    target = str(event.get("target_role") or "role")
    title = f"Debate wake: {sender} -> {target}"
    body_parts = [
        _brief_text(str(event.get("what") or ""), 80),
        _brief_text(str(event.get("will") or ""), 80),
    ]
    body = "\n".join(part for part in body_parts if part)
    try:
        subprocess.run(
            ["notify-send", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except Exception:
        pass


def record_receipt(event: dict[str, Any], *, desktop_notify: bool = True) -> None:
    append_event(event)
    if desktop_notify:
        notify_receipt(event)
