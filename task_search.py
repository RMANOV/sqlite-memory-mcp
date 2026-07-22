"""SmartKey-powered fuzzy task search with CVM personal frequency.

Word-level inverted index + SmartKey ensemble scoring.
Search priority: SmartKey > FTS5 (BM25) > improved substring fallback.
"""

import json
import logging
import math
import os
import re
import sqlite3
import threading
from collections.abc import Callable

_SMARTKEY_AVAILABLE = False
try:
    from smartkey_py import PySmartKeyEngine

    _SMARTKEY_AVAILABLE = True
except ImportError:
    pass

# Words shorter than this are not indexed (articles, prepositions)
_MIN_WORD_LEN = 2


def _scored_fallback(tasks, query, limit):
    """Substring-scored fallback with rank=0.0 for API consistency."""
    scored = [(t, score_task(t, query)) for t in tasks]
    return [
        {**t, "rank": 0.0} for t, s in sorted(scored, key=lambda x: -x[1]) if s > 0
    ][:limit]


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

_INDEX_FIELDS = (
    "id",
    "title",
    "description",
    "notes",
    "project",
    "section",
    "status",
    "priority",
    "due_date",
    "type",
    "updated_at",
)


def build_bounded_index_rows(
    *groups: list[dict],
    limit: int = 300,
    text_chars: int = 800,
) -> list[dict]:
    """Return a deduplicated, bounded projection safe for a resident index."""
    bounded_limit = max(1, int(limit))
    bounded_text = max(1, int(text_chars))
    rows: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for task in group:
            task_id = task.get("id")
            if not task_id or task_id in seen:
                continue
            seen.add(task_id)
            row = {}
            for field in _INDEX_FIELDS:
                value = task.get(field)
                if isinstance(value, str) and len(value) > bounded_text:
                    value = value[:bounded_text]
                row[field] = value
            rows.append(row)
            if len(rows) >= bounded_limit:
                return rows
    return rows


class EntitySearchController:
    """Coalesce background entity searches and identify stale results."""

    def __init__(
        self,
        search: Callable[[str, int], list[dict]],
        emit: Callable[[list[dict], int], None],
        *,
        limit: int,
        on_error: Callable[[Exception], None] | None = None,
    ):
        self._search = search
        self._emit = emit
        self._limit = max(1, int(limit))
        self._on_error = on_error
        self._lock = threading.Lock()
        self._sequence = 0
        self._pending: tuple[int, str] | None = None
        self._running = False

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def is_current(self, sequence: int) -> bool:
        return sequence == self.sequence

    def cancel(self) -> int:
        """Invalidate in-flight results and discard work not yet started."""
        with self._lock:
            self._sequence += 1
            self._pending = None
            return self._sequence

    def request(self, query: str) -> int:
        """Queue the latest query and ensure exactly one worker is draining it."""
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            self._pending = (sequence, query)
            if self._running:
                return sequence
            self._running = True
        threading.Thread(target=self._run, daemon=True).start()
        return sequence

    def _run(self) -> None:
        while True:
            with self._lock:
                pending = self._pending
                self._pending = None
                if pending is None:
                    self._running = False
                    return
            sequence, query = pending
            try:
                results = self._search(query, self._limit)
            except Exception as exc:  # UI background boundary: report, never wedge
                if self._on_error is not None:
                    self._on_error(exc)
                results = []
            self._emit(results, sequence)


def _tokenize(text: str) -> list[str]:
    """Extract lowercase words from text, filtering short ones."""
    return [w.lower() for w in _WORD_RE.findall(text) if len(w) >= _MIN_WORD_LEN]


log = logging.getLogger(__name__)


def score_task(task, query):
    """Field-weighted substring scorer. 0 = no match.

    Scoring hierarchy:
      Title exact phrase  → 200
      Title all words     → 100
      Desc exact phrase   → 80
      Desc all words      → 50
      Notes all words     → 30
      Other fields        → 5  (tiebreaker only)

    ALL query words (len>2) must appear in title+desc+notes to score > 0.
    This eliminates false positives from project/status field leakage.
    """
    q = query.lower()
    title = (task.get("title") or "").lower()
    desc = (task.get("description") or "").lower()
    notes = (task.get("notes") or "").lower()
    core = f"{title} {desc} {notes}"

    # Normalize hyphens/underscores to spaces
    q_norm = _NORMALIZE_RE.sub(" ", q).strip()
    words = [w for w in _SPLIT_RE.split(q_norm) if len(w) >= _MIN_WORD_LEN]

    if not words:
        # Very short query — exact substring only
        if q_norm and q_norm in title:
            return 200
        if q_norm and q_norm in core:
            return 50
        return 0

    # Gate: ALL significant words must appear in core fields
    if not all(w in core for w in words):
        return 0

    # Exact phrase match
    if q_norm in title:
        return 200
    if q_norm in desc:
        return 80

    # All words in title
    if all(w in title for w in words):
        return 100

    # All words in description
    if all(w in desc for w in words):
        return 50

    # All words in notes
    if all(w in notes for w in words):
        return 30

    # Words spread across core fields (already passed the gate)
    return 20


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
        self._state_lock = threading.RLock()
        self._engine_call_lock = threading.RLock()
        self._rebuild_generation = 0

        if _SMARTKEY_AVAILABLE:
            self._engine = PySmartKeyEngine.from_config(_ENGINE_CONFIG)
            self._engine.import_personal(self._cvm_path)

    @property
    def available(self) -> bool:
        with self._state_lock:
            return self._engine is not None

    def rebuild_index(self, tasks: list[dict]) -> None:
        """Build an immutable index snapshot, then publish it atomically."""
        fp = tuple((t["id"], t.get("updated_at", "")) for t in tasks)
        with self._state_lock:
            if fp == self._task_fingerprint:
                return
            self._rebuild_generation += 1
            generation = self._rebuild_generation
            engine_enabled = self._engine is not None

        task_map = {t["id"]: t for t in tasks}
        if not engine_enabled:
            with self._state_lock:
                if generation != self._rebuild_generation:
                    return
                self._task_fingerprint = fp
                self._task_map = task_map
                self._inverted = {}
            return

        engine = PySmartKeyEngine.from_config(_ENGINE_CONFIG)
        engine.import_personal(self._cvm_path)

        # Collect word -> set of task IDs (inverted index)
        word_tasks: dict[str, set[str]] = {}
        for t in tasks:
            tid = t["id"]
            # Tokenize all searchable fields (matches server.py tasks_fts scope)
            words = []
            for field in (
                "title",
                "description",
                "notes",
                "project",
                "section",
                "status",
                "priority",
            ):
                val = t.get(field) or ""
                if val:
                    words.extend(_tokenize(val))
            for w in words:
                word_tasks.setdefault(w, set()).add(tid)

        # Load words into trie with IDF-weighted frequency
        n = len(tasks) or 1
        for word, task_ids in word_tasks.items():
            df = len(task_ids)
            # IDF score: rarer words get higher frequency in trie
            idf = math.log(n / df) + 1.0
            freq = max(1, int(idf * 100))
            engine.load_word(word, freq)

        with self._state_lock:
            if generation != self._rebuild_generation:
                return
            self._task_fingerprint = fp
            self._task_map = task_map
            self._inverted = word_tasks
            self._engine = engine

    def search(
        self,
        query: str,
        tasks: list[dict],
        limit: int = 50,
        conn: sqlite3.Connection | None = None,
        use_vector: bool = True,
    ) -> list[dict]:
        """Fuzzy search tasks. Returns scored + ranked list.

        Priority: SmartKey (fuzzy) > FTS5 (BM25) > substring fallback.
        Pass *conn* to enable FTS5 path when SmartKey is unavailable.
        """
        if not query:
            return tasks[:limit]

        with self._state_lock:
            engine = self._engine
            inverted = self._inverted
            task_map = self._task_map

        if not engine:
            fts_results = None
            vec_results = []
            if conn is not None:
                fts_results = self._fts5_search(conn, query, tasks, limit)
                if use_vector:
                    try:
                        from vec_search import task_vector_search, task_rrf_merge

                        vec_results = task_vector_search(conn, query, limit)
                    except Exception as exc:
                        log.debug(
                            "Vector task search failed for %r, falling back: %s",
                            query,
                            exc,
                            exc_info=True,
                        )

            if fts_results is not None and vec_results:
                # RRF merge FTS5 + vector, filter to task pool
                merged = task_rrf_merge(fts_results, vec_results)
                task_ids = {t["id"] for t in tasks}
                return [t for t in merged if t["id"] in task_ids][:limit]
            elif fts_results is not None:
                return fts_results
            elif vec_results:
                task_ids = {t["id"] for t in tasks}
                return [t for t in vec_results if t["id"] in task_ids][:limit]
            # Final fallback: improved substring scoring
            return _scored_fallback(tasks, query, limit)

        query_words = _tokenize(query)
        if not query_words:
            # Query has no indexable words — try fallback
            return _scored_fallback(tasks, query, limit)

        # For each query word, fuzzy-match against trie and resolve task IDs
        task_scores: dict[str, float] = {}
        task_hits: dict[str, int] = {}  # count of matched query words per task
        task_id_set = {t["id"] for t in tasks}  # only score tasks in current view

        with self._engine_call_lock:
            prediction_groups = [engine.predict(qw, [], 20) for qw in query_words]

        for predictions in prediction_groups:
            # Deduplicate: keep best confidence per matched word
            best: dict[str, float] = {}
            for word, _score, confidence in predictions:
                word_lower = word.lower()
                if confidence > best.get(word_lower, 0):
                    best[word_lower] = confidence

            # Resolve matched words to task IDs via inverted index
            for matched_word, confidence in best.items():
                matching_ids = inverted.get(matched_word, set())
                for tid in matching_ids & task_id_set:
                    task_scores[tid] = task_scores.get(tid, 0) + confidence
                    task_hits[tid] = task_hits.get(tid, 0) + 1

        if not task_scores:
            # SmartKey found nothing — fallback to substring
            return _scored_fallback(tasks, query, limit)

        n_query_words = len(query_words)

        # Boost tasks that match ALL query words (AND logic)
        for tid in task_scores:
            hits = task_hits.get(tid, 0)
            if hits >= n_query_words:
                task_scores[tid] *= 1.5  # full-match bonus

        # Sort by score descending
        ranked_ids = sorted(task_scores, key=lambda tid: -task_scores[tid])[:limit]
        return [task_map[tid] for tid in ranked_ids if tid in task_map]

    @staticmethod
    def _fts5_search(
        conn: sqlite3.Connection,
        query: str,
        tasks: list[dict],
        limit: int,
    ) -> list[dict] | None:
        """FTS5 BM25 search. Returns ranked results or None on failure."""
        tokens = query.split()
        if not tokens:
            return None
        # AND logic: all tokens must match for precise results
        escaped = ['"' + t.replace('"', '""') + '"' for t in tokens]
        fts_q = " AND ".join(escaped)
        try:
            rows = conn.execute(
                "SELECT t.id, t.title, t.description, t.notes, t.status, "
                "t.priority, t.section, t.due_date, t.project, t.parent_id, "
                "t.type, t.updated_at, rank "
                "FROM tasks_fts JOIN tasks t ON tasks_fts.rowid = t.rowid "
                "WHERE tasks_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_q, limit),
            ).fetchall()
        except sqlite3.Error:
            log.debug("FTS5 search failed for %r, falling back", query, exc_info=True)
            return None
        if not rows:
            # FTS5 found nothing with AND — try OR as broadening
            fts_q_or = " OR ".join(escaped)
            try:
                rows = conn.execute(
                    "SELECT t.id, t.title, t.description, t.notes, t.status, "
                    "t.priority, t.section, t.due_date, t.project, t.parent_id, "
                    "t.type, t.updated_at, rank "
                    "FROM tasks_fts JOIN tasks t ON tasks_fts.rowid = t.rowid "
                    "WHERE tasks_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (fts_q_or, limit),
                ).fetchall()
            except sqlite3.Error as e:
                log.debug("FTS5 fallback failed: %s", e, exc_info=True)
                return None
        if not rows:
            return None
        # Filter to only tasks in the current view
        task_ids = {t["id"] for t in tasks}
        return [dict(r) for r in rows if r["id"] in task_ids][:limit]

    def record_open(self, task: dict) -> None:
        """Record that a task was opened (boosts future ranking via CVM)."""
        with self._state_lock:
            engine = self._engine
        if not engine:
            return
        title = task.get("title") or ""
        with self._engine_call_lock:
            for word in _tokenize(title):
                engine.learn(word)

    def save(self) -> None:
        """Persist CVM personal profile to disk."""
        with self._state_lock:
            engine = self._engine
        if engine:
            with self._engine_call_lock:
                engine.export_personal(self._cvm_path)

    def load(self) -> None:
        """Load CVM personal profile from disk."""
        with self._state_lock:
            engine = self._engine
        if engine:
            with self._engine_call_lock:
                engine.import_personal(self._cvm_path)


def merge_tasks_entities(task_results, entity_results, entity_weight=0.7, k=60):
    """Interleave task and entity search results by RRF score.

    Tasks get full 1/(k+rank) weight; entities get entity_weight/(k+rank)
    so tasks rank slightly higher at equal relevance positions.
    """
    scored = []
    for rank, t in enumerate(task_results):
        t["_rrf"] = 1.0 / (k + rank + 1)
        t["_is_entity"] = False
        scored.append(t)
    for rank, e in enumerate(entity_results):
        e["_rrf"] = entity_weight / (k + rank + 1)
        scored.append(e)
    scored.sort(key=lambda x: -x["_rrf"])
    return scored
