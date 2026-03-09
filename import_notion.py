#!/usr/bin/env python3
"""Import tasks/notes from Notion database into sqlite-memory-mcp.

Usage:
    python3 import_notion.py --dry-run          # preview without inserting
    python3 import_notion.py                     # full import
    python3 import_notion.py --db-id <id>        # custom database ID
    python3 import_notion.py --page-size 50      # smaller pages (default: 100)

Requires NOTION_TOKEN env var (or reads from ~/.notion_token).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from db_utils import (
    DB_PATH,
    bulk_conn,
    now_iso,
)

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("import_notion")

# ── Notion API ───────────────────────────────────────────────────────────
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Default: the user's tasks database
DEFAULT_DB_ID = "2f821953-3f39-802f-b6d7-db4199e5367c"


def _get_token() -> str:
    """Read Notion API token from env or file."""
    token = os.environ.get("NOTION_TOKEN", "")
    if token:
        return token
    token_path = Path.home() / ".notion_token"
    if token_path.exists():
        return token_path.read_text().strip()
    raise RuntimeError(
        "NOTION_TOKEN env var not set and ~/.notion_token not found. "
        "Get an integration token from https://www.notion.so/my-integrations"
    )


def _notion_request(method: str, path: str, body: dict | None = None) -> dict:
    """Make a Notion API request."""
    token = _get_token()
    url = f"{NOTION_API}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        log.error("Notion API %s %s → %d: %s", method, path, e.code, body_text)
        raise


# ── Notion content extraction ────────────────────────────────────────────


def _rich_text_to_str(rich_text_list: list[dict]) -> str:
    """Convert Notion rich_text array to plain string."""
    return "".join(rt.get("plain_text", "") for rt in rich_text_list)


def _blocks_to_markdown(blocks: list[dict]) -> str:
    """Convert Notion block objects to markdown text."""
    lines: list[str] = []
    for block in blocks:
        btype = block.get("type", "")
        content = block.get(btype, {})

        if btype == "paragraph":
            lines.append(_rich_text_to_str(content.get("rich_text", [])))
        elif btype in ("heading_1", "heading_2", "heading_3"):
            level = int(btype[-1])
            text = _rich_text_to_str(content.get("rich_text", []))
            lines.append(f"{'#' * level} {text}")
        elif btype == "bulleted_list_item":
            text = _rich_text_to_str(content.get("rich_text", []))
            lines.append(f"- {text}")
        elif btype == "numbered_list_item":
            text = _rich_text_to_str(content.get("rich_text", []))
            lines.append(f"1. {text}")
        elif btype == "to_do":
            text = _rich_text_to_str(content.get("rich_text", []))
            checked = content.get("checked", False)
            lines.append(f"- [{'x' if checked else ' '}] {text}")
        elif btype == "code":
            text = _rich_text_to_str(content.get("rich_text", []))
            lang = content.get("language", "")
            lines.append(f"```{lang}\n{text}\n```")
        elif btype == "quote":
            text = _rich_text_to_str(content.get("rich_text", []))
            lines.append(f"> {text}")
        elif btype == "callout":
            text = _rich_text_to_str(content.get("rich_text", []))
            lines.append(f"> {text}")
        elif btype == "divider":
            lines.append("---")
        elif btype == "toggle":
            text = _rich_text_to_str(content.get("rich_text", []))
            lines.append(f"<details><summary>{text}</summary></details>")
        elif btype == "image":
            url = ""
            if content.get("type") == "external":
                url = content.get("external", {}).get("url", "")
            elif content.get("type") == "file":
                url = content.get("file", {}).get("url", "")
            if url:
                lines.append(f"![image]({url})")
        # Skip unsupported block types silently

    return "\n".join(lines)


def _fetch_page_blocks(page_id: str) -> list[dict]:
    """Fetch all blocks (children) of a Notion page, handling pagination."""
    all_blocks: list[dict] = []
    cursor = None

    while True:
        path = f"/blocks/{page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        data = _notion_request("GET", path)
        all_blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return all_blocks


# ── Property mapping ─────────────────────────────────────────────────────

# Map Notion property names to task fields (case-insensitive matching)
_PRIORITY_MAP = {
    "critical": "critical", "high": "high", "medium": "medium", "low": "low",
    "p0": "critical", "p1": "high", "p2": "medium", "p3": "low",
    "urgent": "critical",
}

_STATUS_MAP = {
    "not started": "not_started", "not_started": "not_started",
    "in progress": "in_progress", "in_progress": "in_progress",
    "done": "done", "complete": "done", "completed": "done",
    "archived": "archived", "cancelled": "cancelled", "canceled": "cancelled",
}

_SECTION_MAP = {
    "inbox": "inbox", "today": "today", "next": "next",
    "someday": "someday", "waiting": "waiting",
    "backlog": "someday", "later": "someday",
}


def _extract_property(props: dict, name: str) -> str | None:
    """Extract a property value by name (case-insensitive)."""
    for key, val in props.items():
        if key.lower() == name.lower():
            ptype = val.get("type", "")
            if ptype == "title":
                return _rich_text_to_str(val.get("title", []))
            elif ptype == "rich_text":
                return _rich_text_to_str(val.get("rich_text", []))
            elif ptype == "select":
                sel = val.get("select")
                return sel.get("name", "") if sel else None
            elif ptype == "multi_select":
                return ", ".join(s.get("name", "") for s in val.get("multi_select", []))
            elif ptype == "date":
                d = val.get("date")
                return d.get("start") if d else None
            elif ptype == "checkbox":
                return str(val.get("checkbox", False))
            elif ptype == "number":
                return str(val.get("number", ""))
            elif ptype == "status":
                st = val.get("status")
                return st.get("name", "") if st else None
    return None


def _map_notion_page(page: dict) -> dict:
    """Map a Notion page to a sqlite-memory-mcp task dict."""
    props = page.get("properties", {})

    # Title — try common property names
    title = None
    for name in ("Name", "Title", "Task", "name", "title"):
        title = _extract_property(props, name)
        if title:
            break
    if not title:
        # Fallback: find any title-type property
        for key, val in props.items():
            if val.get("type") == "title":
                title = _rich_text_to_str(val.get("title", []))
                break
    title = title or "Untitled"

    # Status
    raw_status = None
    for name in ("Status", "status", "State"):
        raw_status = _extract_property(props, name)
        if raw_status:
            break
    status = _STATUS_MAP.get((raw_status or "").lower(), "not_started")

    # Priority
    raw_priority = None
    for name in ("Priority", "priority", "Urgency"):
        raw_priority = _extract_property(props, name)
        if raw_priority:
            break
    priority = _PRIORITY_MAP.get((raw_priority or "").lower(), "medium")

    # Section
    raw_section = None
    for name in ("Section", "section", "Category", "Area"):
        raw_section = _extract_property(props, name)
        if raw_section:
            break
    section = _SECTION_MAP.get((raw_section or "").lower(), "inbox")

    # Due date
    due_date = None
    for name in ("Due", "Due Date", "due_date", "Deadline", "Date"):
        due_date = _extract_property(props, name)
        if due_date:
            # Normalize to YYYY-MM-DD
            due_date = due_date[:10] if len(due_date) >= 10 else due_date
            break

    # Project
    project = None
    for name in ("Project", "project", "Area", "Team"):
        project = _extract_property(props, name)
        if project:
            break

    # Type: note vs task
    task_type = "task"
    raw_type = _extract_property(props, "Type") or _extract_property(props, "type")
    if raw_type and raw_type.lower() in ("note", "notes"):
        task_type = "note"

    # Timestamps
    created_at = page.get("created_time", now_iso())
    updated_at = page.get("last_edited_time", now_iso())

    # Dedup hash based on title + created_time
    dedup_key = hashlib.sha256(
        f"{title}:{created_at}".encode()
    ).hexdigest()[:12]
    task_id = f"notion-{dedup_key}"

    return {
        "id": task_id,
        "title": title,
        "status": status,
        "priority": priority,
        "section": section,
        "due_date": due_date,
        "project": project,
        "type": task_type,
        "created_at": created_at,
        "updated_at": updated_at,
        "notion_page_id": page["id"],  # kept for content fetch, not stored
    }


# ── Main import logic ────────────────────────────────────────────────────


def fetch_all_pages(db_id: str, page_size: int = 100) -> list[dict]:
    """Fetch all pages from a Notion database with pagination."""
    all_pages: list[dict] = []
    cursor = None

    while True:
        body: dict[str, Any] = {"page_size": page_size}
        if cursor:
            body["start_cursor"] = cursor

        data = _notion_request("POST", f"/databases/{db_id}/query", body)
        results = data.get("results", [])
        all_pages.extend(results)
        log.info("Fetched %d pages (total: %d)", len(results), len(all_pages))

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return all_pages


def import_pages(
    pages: list[dict],
    *,
    fetch_content: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    """Import Notion pages into the tasks table.

    Returns stats: {"imported": N, "skipped": N, "errors": N}
    """
    tasks_to_insert: list[dict] = []
    content_map: dict[str, str] = {}  # task_id → description
    notes_map: dict[str, str] = {}    # task_id → notes (from comments)
    stats = {"imported": 0, "skipped": 0, "errors": 0, "total": len(pages)}

    # Map all pages first
    for page in pages:
        try:
            task = _map_notion_page(page)
            tasks_to_insert.append(task)
        except Exception as exc:
            log.warning("Failed to map page %s: %s", page.get("id", "?"), exc)
            stats["errors"] += 1

    # Fetch content for each page (block children)
    if fetch_content:
        for i, task in enumerate(tasks_to_insert):
            page_id = task.pop("notion_page_id", None)
            if not page_id:
                continue
            try:
                blocks = _fetch_page_blocks(page_id)
                md = _blocks_to_markdown(blocks)
                if md.strip():
                    content_map[task["id"]] = md
                if (i + 1) % 50 == 0:
                    log.info("Content fetched: %d/%d", i + 1, len(tasks_to_insert))
            except Exception as exc:
                log.warning("Failed to fetch blocks for %s: %s", task["title"], exc)
    else:
        for task in tasks_to_insert:
            task.pop("notion_page_id", None)

    if dry_run:
        log.info("DRY RUN — would import %d tasks", len(tasks_to_insert))
        for t in tasks_to_insert[:10]:
            desc_len = len(content_map.get(t["id"], ""))
            log.info(
                "  [%s] %s (status=%s, priority=%s, desc=%d chars)",
                t["id"], t["title"], t["status"], t["priority"], desc_len,
            )
        if len(tasks_to_insert) > 10:
            log.info("  ... and %d more", len(tasks_to_insert) - 10)
        stats["imported"] = len(tasks_to_insert)
        return stats

    # Bulk insert + FTS rebuild in a single transaction
    with bulk_conn() as conn:
        for task in tasks_to_insert:
            task_id = task["id"]
            description = content_map.get(task_id)
            notes = notes_map.get(task_id)

            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO tasks "
                    "(id, title, description, status, priority, section, due_date, "
                    "project, type, notes, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        task["title"],
                        description,
                        task["status"],
                        task["priority"],
                        task["section"],
                        task["due_date"],
                        task["project"],
                        task["type"],
                        notes,
                        task["created_at"],
                        task["updated_at"],
                    ),
                )
                if cur.rowcount > 0:
                    stats["imported"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as exc:
                log.warning("Insert failed for %s: %s", task["title"], exc)
                stats["errors"] += 1

        # Rebuild FTS index within the same transaction
        if stats["imported"] > 0:
            log.info("Rebuilding tasks_fts index...")
            conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES('rebuild')")
            log.info("FTS rebuild complete")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Import Notion tasks into sqlite-memory-mcp")
    parser.add_argument("--db-id", default=DEFAULT_DB_ID, help="Notion database ID")
    parser.add_argument("--page-size", type=int, default=100, help="Pages per API request")
    parser.add_argument("--dry-run", action="store_true", help="Preview without inserting")
    parser.add_argument("--no-content", action="store_true", help="Skip fetching page content")
    parser.add_argument("--db-path", default=None, help="Override SQLite DB path")
    args = parser.parse_args()

    if args.db_path:
        os.environ["SQLITE_MEMORY_DB"] = args.db_path

    log.info("Fetching pages from Notion database %s...", args.db_id)
    pages = fetch_all_pages(args.db_id, page_size=args.page_size)
    log.info("Found %d pages in Notion", len(pages))

    if not pages:
        log.info("No pages to import")
        return

    stats = import_pages(
        pages,
        fetch_content=not args.no_content,
        dry_run=args.dry_run,
    )

    log.info(
        "Import complete: %d imported, %d skipped (duplicates), %d errors (of %d total)",
        stats["imported"], stats["skipped"], stats["errors"], stats["total"],
    )

    # Show DB stats
    if not args.dry_run:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        db_size = Path(DB_PATH).stat().st_size / (1024 * 1024)
        conn.close()
        log.info("DB now has %d tasks, size: %.1f MB", task_count, db_size)


if __name__ == "__main__":
    main()
