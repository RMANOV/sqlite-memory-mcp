"""
task_report.py — GitHub Pages Kanban board generator for SQLite Memory tasks.

Reads tasks from ~/.claude/memory/memory.db and generates a self-contained
index.html Kanban board saved to the bridge repo for GitHub Pages.

Usage:
    python task_report.py
"""

import os
import sqlite3
from datetime import date, datetime

DB_PATH = os.path.expanduser("~/.claude/memory/memory.db")
BRIDGE_REPO = os.path.expanduser("~/.claude/memory/bridge")

SECTIONS = ["today", "inbox", "next", "waiting", "someday"]
SECTION_LABELS = {
    "today": "Today",
    "inbox": "Inbox",
    "next": "Next",
    "waiting": "Waiting",
    "someday": "Someday",
}

PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _get_tasks() -> tuple[list[dict], set[str]]:
    """
    Return (tasks_list, parent_ids_set).

    tasks_list: all non-archived, non-cancelled tasks as dicts.
    parent_ids_set: set of IDs that are referenced as parent_id by any task.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, title, status, priority, section, due_date,
                   project, parent_id, notes, created_at, updated_at
            FROM tasks
            WHERE status NOT IN ('archived', 'cancelled')
            ORDER BY created_at
            """
        )
        tasks = [dict(row) for row in cur.fetchall()]

        # Collect IDs that appear as parent_id (have children)
        parent_ids: set[str] = set()
        cur.execute("SELECT DISTINCT parent_id FROM tasks WHERE parent_id IS NOT NULL")
        for row in cur.fetchall():
            parent_ids.add(row[0])

        return tasks, parent_ids
    finally:
        conn.close()


def _sort_key(task: dict) -> tuple:
    priority = PRIORITY_ORDER.get(task.get("priority", "low"), 3)
    due = task.get("due_date") or "9999-12-31"
    return (priority, due)


def _html_escape(text: str) -> str:
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_card(task: dict, today_str: str, parent_ids: set[str]) -> str:
    title = _html_escape(task.get("title") or "Untitled")
    priority = (task.get("priority") or "low").lower()
    due_date = task.get("due_date")
    project = task.get("project")
    task_id = task.get("id", "")
    has_children = task_id in parent_ids

    is_overdue = bool(due_date and due_date < today_str)

    card_class = "card"
    if is_overdue:
        card_class += " card--overdue"

    priority_label = priority.upper()
    priority_class = f"badge badge--{priority}"

    badges_html = f'<span class="{priority_class}">{priority_label}</span>'

    if is_overdue:
        badges_html += ' <span class="badge badge--overdue">OVERDUE</span>'

    if has_children:
        badges_html += ' <span class="badge badge--subtask">SUBTASKS</span>'

    due_html = ""
    if due_date:
        due_html = f'<div class="card__due">Due: {_html_escape(due_date)}</div>'

    project_html = ""
    if project:
        project_html = f'<div class="card__project">{_html_escape(project)}</div>'

    return f"""
      <div class="{card_class}">
        <div class="card__title">{title}</div>
        <div class="card__meta">
          {badges_html}
        </div>
        {due_html}
        {project_html}
      </div>"""


def _render_column(
    section: str, tasks: list[dict], today_str: str, parent_ids: set[str]
) -> str:
    label = SECTION_LABELS.get(section, section.title())
    count = len(tasks)

    if tasks:
        cards_html = "\n".join(_render_card(t, today_str, parent_ids) for t in tasks)
    else:
        cards_html = '<div class="empty-state">No tasks</div>'

    return f"""
    <div class="column">
      <div class="column__header">
        <span class="column__title">{label}</span>
        <span class="column__count">{count}</span>
      </div>
      <div class="column__body">
        {cards_html}
      </div>
    </div>"""


def _build_html(tasks: list[dict], parent_ids: set[str]) -> str:
    today = date.today()
    today_str = today.isoformat()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Group tasks by section
    by_section: dict[str, list[dict]] = {s: [] for s in SECTIONS}
    for task in tasks:
        section = (task.get("section") or "inbox").lower()
        if section not in by_section:
            section = "inbox"
        by_section[section].append(task)

    # Sort within each section
    for section in SECTIONS:
        by_section[section].sort(key=_sort_key)

    total = len(tasks)
    overdue = sum(1 for t in tasks if t.get("due_date") and t["due_date"] < today_str)

    columns_html = "".join(
        _render_column(s, by_section[s], today_str, parent_ids) for s in SECTIONS
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Task Board — SQLite Memory</title>
  <style>
    /* ── Reset ── */
    *, *::before, *::after {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}

    /* ── Base ── */
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                   "Helvetica Neue", Arial, sans-serif;
      font-size: 14px;
      background: #f0f4f8;
      color: #1a2332;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }}

    /* ── Page header ── */
    .page-header {{
      background: #1a2332;
      color: #ffffff;
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
    }}

    .page-header__title {{
      font-size: 20px;
      font-weight: 700;
      letter-spacing: 0.5px;
    }}

    /* ── Summary bar ── */
    .summary-bar {{
      background: #2d3748;
      color: #f7fafc;
      padding: 8px 24px;
      display: flex;
      gap: 24px;
      font-size: 13px;
      flex-wrap: wrap;
    }}

    .summary-bar__item {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}

    .summary-bar__label {{
      color: #a0aec0;
    }}

    .summary-bar__value {{
      font-weight: 600;
      color: #ffffff;
    }}

    .summary-bar__value--overdue {{
      color: #fc8181;
    }}

    /* ── Board ── */
    .board {{
      display: flex;
      gap: 16px;
      padding: 20px 24px;
      overflow-x: auto;
      flex: 1;
      align-items: flex-start;
    }}

    /* ── Column ── */
    .column {{
      flex: 0 0 260px;
      min-width: 220px;
      max-width: 300px;
      background: #e2e8f0;
      border-radius: 6px;
      display: flex;
      flex-direction: column;
    }}

    .column__header {{
      background: #2d3748;
      color: #ffffff;
      padding: 10px 14px;
      border-radius: 6px 6px 0 0;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}

    .column__title {{
      font-weight: 700;
      font-size: 13px;
      letter-spacing: 0.8px;
      text-transform: uppercase;
    }}

    .column__count {{
      background: #4a5568;
      color: #f7fafc;
      font-size: 12px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 10px;
      min-width: 24px;
      text-align: center;
    }}

    .column__body {{
      padding: 10px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      min-height: 80px;
    }}

    /* ── Card ── */
    .card {{
      background: #ffffff;
      border-radius: 4px;
      padding: 10px 12px;
      border-left: 3px solid #cbd5e0;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    }}

    .card--overdue {{
      border-left-color: #e53e3e;
    }}

    .card__title {{
      font-size: 13px;
      font-weight: 600;
      color: #1a2332;
      line-height: 1.4;
      margin-bottom: 6px;
      word-break: break-word;
    }}

    .card__meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      margin-bottom: 4px;
    }}

    .card__due {{
      font-size: 11px;
      color: #4a5568;
      margin-top: 4px;
    }}

    .card__project {{
      font-size: 11px;
      color: #718096;
      margin-top: 2px;
      font-style: italic;
    }}

    /* ── Badges ── */
    .badge {{
      font-size: 10px;
      font-weight: 700;
      padding: 2px 6px;
      border-radius: 3px;
      letter-spacing: 0.4px;
      display: inline-block;
    }}

    .badge--critical {{
      background: #fff5f5;
      color: #e53e3e;
      border: 1px solid #feb2b2;
    }}

    .badge--high {{
      background: #fffaf0;
      color: #dd6b20;
      border: 1px solid #fbd38d;
    }}

    .badge--medium {{
      background: #ebf8ff;
      color: #2b6cb0;
      border: 1px solid #bee3f8;
    }}

    .badge--low {{
      background: #f7fafc;
      color: #718096;
      border: 1px solid #e2e8f0;
    }}

    .badge--overdue {{
      background: #fff5f5;
      color: #c53030;
      border: 1px solid #fc8181;
    }}

    .badge--subtask {{
      background: #f0fff4;
      color: #276749;
      border: 1px solid #9ae6b4;
    }}

    /* ── Empty state ── */
    .empty-state {{
      color: #a0aec0;
      font-size: 12px;
      text-align: center;
      padding: 16px 8px;
      font-style: italic;
    }}

    /* ── Footer ── */
    .page-footer {{
      background: #2d3748;
      color: #a0aec0;
      font-size: 11px;
      padding: 10px 24px;
      text-align: right;
    }}

    /* ── Responsive ── */
    @media (max-width: 600px) {{
      .board {{
        flex-direction: column;
        padding: 12px;
      }}
      .column {{
        flex: none;
        width: 100%;
        max-width: 100%;
      }}
      .page-header {{
        padding: 12px 16px;
      }}
      .summary-bar {{
        padding: 8px 16px;
      }}
    }}
  </style>
</head>
<body>

  <header class="page-header">
    <div class="page-header__title">Task Board — SQLite Memory</div>
  </header>

  <div class="summary-bar">
    <div class="summary-bar__item">
      <span class="summary-bar__label">Total tasks:</span>
      <span class="summary-bar__value">{total}</span>
    </div>
    <div class="summary-bar__item">
      <span class="summary-bar__label">Overdue:</span>
      <span class="summary-bar__value{"--overdue" if overdue else ""}">{overdue}</span>
    </div>
  </div>

  <div class="board">
    {columns_html}
  </div>

  <footer class="page-footer">
    Last updated: {now_str}
  </footer>

</body>
</html>"""


def generate_report() -> str:
    """Generate Kanban HTML and save to bridge repo. Returns path."""
    tasks, parent_ids = _get_tasks()
    html = _build_html(tasks, parent_ids)

    output_path = os.path.join(BRIDGE_REPO, "index.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path


if __name__ == "__main__":
    path = generate_report()
    print(f"Report generated: {path}")
