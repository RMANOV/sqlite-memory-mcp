import task_report


def test_render_card_escapes_content_and_preserves_badges(monkeypatch):
    monkeypatch.setattr(task_report, "_is_overdue", lambda due_date: True)
    task = {
        "id": "parent",
        "title": """<Draft & "review">'""",
        "priority": "unknown",
        "due_date": "2026-07-01",
        "project": "<client>",
        "type": "note",
    }

    rendered = task_report._render_card(task, {"parent"})

    assert "&lt;Draft &amp; &quot;review&quot;&gt;&#x27;" in rendered
    assert "&lt;client&gt;" in rendered
    assert "badge--low" in rendered
    assert "badge--overdue" in rendered
    assert "badge--subtask" in rendered
    assert "badge--note" in rendered


def test_build_html_groups_unknown_sections_into_inbox(monkeypatch):
    monkeypatch.setattr(task_report, "_is_overdue", lambda due_date: False)
    rendered = task_report._build_html(
        [
            {
                "id": "task-1",
                "title": "Unsorted task",
                "priority": "medium",
                "section": "unknown",
                "type": "task",
            }
        ],
        set(),
    )

    assert "Total tasks:" in rendered
    assert "Unsorted task" in rendered
    assert '<span class="column__title">Inbox</span>' in rendered
