# Task Tray — SQLite Task Manager

> System tray widget with dual mode: compact popup (daily) + full window (planning)

## Use Case

Daily dashboard — виждам Today + Overdue на един поглед, отбелязвам готовите,
добавям нови по време на работа. При нужда — отварям full window за planning.

## Architecture

```
task_tray.py (single file, ~600 lines)
    +-- QSystemTrayIcon — always running
    +-- TrayPopup(QWidget) — compact, ~400x450px
    |     +-- Today section (tasks with section="today")
    |     +-- Overdue section (due_date < today)
    |     +-- Checkbox -> mark done
    |     +-- [+ Quick Add] -> inline input
    |     +-- [Open full] -> launches FullWindow
    +-- FullWindow(QMainWindow) — on demand
          +-- Tab bar: Today | Inbox | Next | All
          +-- Task list (QListWidget or custom)
          +-- Inline edit (click to change title/priority/section)
          +-- [+ Add] button -> dialog with all fields
          +-- Status bar: "12 tasks, 4 overdue"
```

## Data Layer

- Direct `sqlite3` connection to `~/.claude/memory/memory.db`
- WAL mode + busy_timeout=5000 (same as server.py)
- Auto-refresh every 30s or on window focus
- No MCP dependency — reads/writes DB directly
- Bridge sync handled by `bridge_auto_sync.py` hook (already deployed)

## Dependencies

- **PyQt6** (already installed — 6.10.2)
- **sqlite3** (stdlib)
- Nothing else

## Visual Style

- Business palette: dark navy #1a2332, grey, black, white
- Maximum contrast
- Accents only for critical: red=overdue, green=done
- Matches Kanban board CSS colors

## Key Interactions

| Action           | Where        | How                           |
|------------------|-------------|-------------------------------|
| Mark done        | Popup + Full | Checkbox click                |
| Add task         | Popup        | Inline text + Enter           |
| Add (detailed)   | Full         | Dialog with all fields        |
| Change section   | Full         | Dropdown                      |
| Change priority  | Full         | Click badge -> cycle          |
| Delete           | Full         | Right-click -> Delete         |
| Edit title       | Full         | Double-click -> inline        |

## Task Schema (SQLite)

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'not_started',
    section TEXT DEFAULT 'inbox',
    priority TEXT DEFAULT 'medium',
    due_date TEXT,
    project TEXT,
    parent_id TEXT,
    notes TEXT,
    recurring TEXT,
    created_at TEXT,
    updated_at TEXT
)
```

**Status values:** not_started, in_progress, pending, done, archived, cancelled
**Section values:** today, inbox, next, waiting, someday
**Priority values:** critical, high, medium, low

## Tray Icon

- Badge overlay showing count of overdue tasks (red circle with number)
- Tooltip: "Tasks: 12 | Overdue: 4"
- Left click: toggle popup
- Right click: context menu (Open full, Add task, Quit)

## Popup Behavior

- Appears near tray icon (platform-aware positioning)
- Auto-hides on focus loss
- Stays on top of other windows
- Max ~10 tasks visible, scrollable

## Full Window Behavior

- Opens centered, ~800x600
- Remembers last size/position
- Can coexist with popup
- Close -> hide (don't quit), minimize to tray

## Not In Scope (YAGNI)

- Drag-and-drop reordering
- Subtask tree view
- Calendar view
- Recurring task creation (handled by scripts)
- Sync UI (bridge sync is automatic)
- Search (small dataset, visual scan sufficient)
