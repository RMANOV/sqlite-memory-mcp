import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from tray_filters import FilterMixin


def _empty_filters():
    return {"priority": set(), "due": set(), "project": set()}


class _SearchStub:
    def search(self, query, tasks, conn=None, use_vector=False):
        return tasks


class _DummyWindow(FilterMixin):
    def __init__(self):
        self._search_text = ""
        self._search_engine = _SearchStub()
        self._active_filters = _empty_filters()
        self._excluded_filters = _empty_filters()


def test_filter_matches_project_aliases_using_canonical_name():
    window = _DummyWindow()
    window._active_filters["project"] = {"mapping-studio"}

    tasks = [
        {"id": "a", "project": "mapping_studio"},
        {"id": "b", "project": "mapping-studio"},
        {"id": "c", "project": "other"},
    ]

    filtered = window._filter(tasks)

    assert {task["id"] for task in filtered} == {"a", "b"}


def test_filter_excludes_project_aliases_using_canonical_name():
    window = _DummyWindow()
    window._excluded_filters["project"] = {"smartkey"}

    tasks = [
        {"id": "a", "project": "SmartKey"},
        {"id": "b", "project": "smartkey"},
        {"id": "c", "project": "other"},
    ]

    filtered = window._filter(tasks)

    assert {task["id"] for task in filtered} == {"c"}
