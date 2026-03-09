"""SmartKey-powered fuzzy task search with CVM personal frequency.

Word-level inverted index + SmartKey ensemble scoring.
Gracefully falls back to substring matching if smartkey_py is unavailable.
"""

import json
import math
import os
import re

_SMARTKEY_AVAILABLE = False
try:
    from smartkey_py import PySmartKeyEngine

    _SMARTKEY_AVAILABLE = True
except ImportError:
    pass

# Words shorter than this are not indexed (articles, prepositions)
_MIN_WORD_LEN = 2

# SmartKey config: corpus IDF high, markov off, personal CVM active
_ENGINE_CONFIG = json.dumps(
    {
        "weights": {"corpus": 0.7, "markov": 0.0, "personal": 0.3},
        "tuning": {
            "cvm_initial_size": 1000,
            "cvm_max_size": 5000,
            "cvm_decay_lambda": 0.0001,
            "fuzzy_max_edits": 2,
            "fuzzy_discounts": [1.0, 0.7, 0.4],
        },
    }
)

# Regex for tokenizing task titles into words
_WORD_RE = re.compile(r"[a-zA-Z0-9\u0400-\u04FF]+")
# Pre-compiled patterns for hyphen/underscore normalization in search
_NORMALIZE_RE = re.compile(r"[-_]+")
_SPLIT_RE = re.compile(r"[\s\-_]+")


def _tokenize(text: str) -> list[str]:
    """Extract lowercase words from text, filtering short ones."""
    return [w.lower() for w in _WORD_RE.findall(text) if len(w) >= _MIN_WORD_LEN]


def score_task(task, query):
    """Substring scorer for task relevance. 0 = no match."""
    q = query.lower()
    title = (task.get("title") or "").lower()
    combined = (
        f"{title} {(task.get('description') or '').lower()} "
        f"{task.get('priority', '')} {task.get('project', '')} "
        f"{task.get('due_date', '')} {task.get('section', '')} {task.get('status', '')}"
    )
    # Normalize hyphens/underscores to spaces for substring matching
    q_normalized = _NORMALIZE_RE.sub(" ", q).strip()
    if q_normalized and q_normalized in title:
        return 100
    if q_normalized != q and q in title:
        return 100
    if q_normalized and q_normalized in combined:
        return 50
    if q_normalized != q and q in combined:
        return 50
    words = [w for w in _SPLIT_RE.split(q) if w]
    if len(words) > 1:
        matched = sum(1 for w in words if w in combined)
        if matched == len(words):
            return 30
        if matched > 0:
            return 10 * matched
    return 0


class TaskSearchEngine:
    """SmartKey-powered fuzzy task search with CVM personal frequency."""

    def __init__(self, cvm_path: str | None = None):
        self._cvm_path = os.path.expanduser(
            cvm_path or "~/.claude/memory/task_cvm.json"
        )
        self._engine: PySmartKeyEngine | None = None
        self._inverted: dict[str, set[str]] = {}  # word -> {task_id, ...}
        self._task_map: dict[str, dict] = {}  # id -> task dict
        self._task_fingerprint: tuple | None = None

        if _SMARTKEY_AVAILABLE:
            self._engine = PySmartKeyEngine.from_config(_ENGINE_CONFIG)
            self._engine.import_personal(self._cvm_path)

    @property
    def available(self) -> bool:
        return self._engine is not None

    def rebuild_index(self, tasks: list[dict]) -> None:
        """Tokenize task titles, build inverted index, load trie."""
        fp = tuple((t["id"], t.get("updated_at", "")) for t in tasks)
        if fp == self._task_fingerprint:
            return
        self._task_fingerprint = fp

        # Build task map
        self._task_map = {t["id"]: t for t in tasks}

        if not self._engine:
            return

        # Rebuild engine (new instance to clear old trie)
        self._engine = PySmartKeyEngine.from_config(_ENGINE_CONFIG)
        self._engine.import_personal(self._cvm_path)

        # Collect word -> set of task IDs (inverted index)
        word_tasks: dict[str, set[str]] = {}
        for t in tasks:
            tid = t["id"]
            title = t.get("title") or ""
            project = t.get("project") or ""
            # Tokenize title + project (searchable fields)
            words = _tokenize(title) + _tokenize(project)
            for w in words:
                word_tasks.setdefault(w, set()).add(tid)

        self._inverted = word_tasks

        # Load words into trie with IDF-weighted frequency
        n = len(tasks) or 1
        for word, task_ids in word_tasks.items():
            df = len(task_ids)
            # IDF score: rarer words get higher frequency in trie
            idf = math.log(n / df) + 1.0
            freq = max(1, int(idf * 100))
            self._engine.load_word(word, freq)

    def search(self, query: str, tasks: list[dict], limit: int = 50) -> list[dict]:
        """Fuzzy search tasks. Returns scored + ranked list."""
        if not query:
            return tasks[:limit]

        if not self._engine:
            # Fallback to substring matching
            scored = [(t, score_task(t, query)) for t in tasks]
            return [t for t, s in sorted(scored, key=lambda x: -x[1]) if s > 0][:limit]

        query_words = _tokenize(query)
        if not query_words:
            # Query has no indexable words — try fallback
            scored = [(t, score_task(t, query)) for t in tasks]
            return [t for t, s in sorted(scored, key=lambda x: -x[1]) if s > 0][:limit]

        # For each query word, fuzzy-match against trie and resolve task IDs
        task_scores: dict[str, float] = {}
        task_hits: dict[str, int] = {}  # count of matched query words per task
        task_id_set = {t["id"] for t in tasks}  # only score tasks in current view

        for qw in query_words:
            # SmartKey predict returns (word, score, confidence)
            predictions = self._engine.predict(qw, [], 20)

            # Deduplicate: keep best confidence per matched word
            best: dict[str, float] = {}
            for word, _score, confidence in predictions:
                word_lower = word.lower()
                if confidence > best.get(word_lower, 0):
                    best[word_lower] = confidence

            # Resolve matched words to task IDs via inverted index
            for matched_word, confidence in best.items():
                matching_ids = self._inverted.get(matched_word, set())
                for tid in matching_ids & task_id_set:
                    task_scores[tid] = task_scores.get(tid, 0) + confidence
                    task_hits[tid] = task_hits.get(tid, 0) + 1

        if not task_scores:
            # SmartKey found nothing — fallback to substring
            scored = [(t, score_task(t, query)) for t in tasks]
            return [t for t, s in sorted(scored, key=lambda x: -x[1]) if s > 0][:limit]

        n_query_words = len(query_words)

        # Boost tasks that match ALL query words (AND logic)
        for tid in task_scores:
            hits = task_hits.get(tid, 0)
            if hits >= n_query_words:
                task_scores[tid] *= 1.5  # full-match bonus

        # Sort by score descending
        ranked_ids = sorted(task_scores, key=lambda tid: -task_scores[tid])[:limit]
        return [self._task_map[tid] for tid in ranked_ids if tid in self._task_map]

    def record_open(self, task: dict) -> None:
        """Record that a task was opened (boosts future ranking via CVM)."""
        if not self._engine:
            return
        title = task.get("title") or ""
        for word in _tokenize(title):
            self._engine.learn(word)

    def save(self) -> None:
        """Persist CVM personal profile to disk."""
        if self._engine:
            self._engine.export_personal(self._cvm_path)

    def load(self) -> None:
        """Load CVM personal profile from disk."""
        if self._engine:
            self._engine.import_personal(self._cvm_path)
