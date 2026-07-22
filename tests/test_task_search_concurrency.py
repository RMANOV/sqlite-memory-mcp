"""Concurrency regressions for atomic TaskSearchEngine index publication."""

import threading

import task_search


class _BlockingSmartKeyEngine:
    blocked_word = ""
    build_started = threading.Event()
    build_release = threading.Event()

    def __init__(self):
        self.words: set[str] = set()

    @classmethod
    def from_config(cls, config):
        return cls()

    def import_personal(self, path):
        return None

    def load_word(self, word, frequency):
        if word == type(self).blocked_word:
            type(self).build_started.set()
            assert type(self).build_release.wait(5)
        self.words.add(word)

    def predict(self, word, context, limit):
        return [(word, 1.0, 1.0)] if word in self.words else []

    def learn(self, word):
        self.words.add(word)

    def export_personal(self, path):
        return None


def test_rebuild_publishes_complete_snapshot_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(task_search, "_SMARTKEY_AVAILABLE", True)
    monkeypatch.setattr(
        task_search, "PySmartKeyEngine", _BlockingSmartKeyEngine, raising=False
    )
    _BlockingSmartKeyEngine.blocked_word = ""
    _BlockingSmartKeyEngine.build_started.clear()
    _BlockingSmartKeyEngine.build_release.clear()

    search = task_search.TaskSearchEngine(str(tmp_path / "cvm.json"))
    old_tasks = [{"id": "old", "title": "oldword", "updated_at": "1"}]
    new_tasks = [{"id": "new", "title": "newword", "updated_at": "2"}]
    search.rebuild_index(old_tasks)
    old_fingerprint = search._task_fingerprint

    _BlockingSmartKeyEngine.blocked_word = "newword"
    worker = threading.Thread(target=search.rebuild_index, args=(new_tasks,))
    worker.start()
    assert _BlockingSmartKeyEngine.build_started.wait(5)

    try:
        assert search._task_fingerprint == old_fingerprint
        assert [row["id"] for row in search.search("oldword", old_tasks)] == ["old"]
    finally:
        _BlockingSmartKeyEngine.build_release.set()
        worker.join(5)

    assert not worker.is_alive()
    assert search._task_fingerprint != old_fingerprint
    assert [row["id"] for row in search.search("newword", new_tasks)] == ["new"]
