"""The 64-token FTS cap must not act as a language filter.

``tokenize_for_similarity`` returns a *set*; both FTS helpers order it and keep
a 64-token prefix.  Ordering by codepoint puts every ASCII token ahead of every
Cyrillic one, so on a Bulgarian corpus the cap was consumed by digits and Latin
noise before it ever reached a content word — the query hit the index carrying
no Bulgarian at all.  These tests pin the length-first ordering that fixes it.

Token lists are constructed directly here.  Nothing in this module opens, reads
or writes any on-disk database.
"""

from __future__ import annotations

import random
import sqlite3

import pytest

import db_utils
from link_suggestions import _safe_fts_query
from memory_thread_clustering import _safe_task_fts_query


@pytest.fixture(autouse=True)
def _bootstrap_stopwords():
    """Pin the tokenizer's stopword set so these assertions stay order-independent.

    ``tokenize_for_similarity`` consults a process-global learned set that any
    other test in the same session may install.  ``getattr`` keeps this a no-op
    on builds that predate that mechanism.
    """
    reset = getattr(db_utils, "set_similarity_stopwords", None)
    if reset is None:
        yield
        return
    reset(None)
    try:
        yield
    finally:
        reset(None)

# Both helpers implement the same cap policy; every behavioural test runs
# against both so the duplicated implementations cannot drift apart.
HELPERS = [
    pytest.param(_safe_fts_query, id="link_suggestions"),
    pytest.param(_safe_task_fts_query, id="memory_thread_clustering"),
]

TOKEN_CAP = 64

# The Bulgarian content words a suggestion query exists to carry.
CYRILLIC_CONTENT = (
    "конфигурация",
    "автоматизация",
    "разпределение",
    "потребителски",
    "интерфейс",
    "клавиатура",
)

# Short ASCII noise of the kind that really precedes Cyrillic in codepoint
# order: numeric ids and three-letter latin tokens.  90 of them, so the
# document is well past the 64-token cap on noise alone.
ASCII_NOISE = tuple(f"{index:03d}" for index in range(60)) + tuple(
    f"ab{letter}" for letter in "abcdefghijklmnopqrstuvwxyz"
) + ("log", "url", "api", "sql")


def _mixed_document(*, seed: int = 0) -> str:
    """A >64-token document mixing short ASCII noise with long Cyrillic words."""
    words = [*ASCII_NOISE, *CYRILLIC_CONTENT]
    if seed:
        random.Random(seed).shuffle(words)
    return " ".join(words)


def _quoted_terms(query: str) -> list[str]:
    """Recover the tokens FTS5 will actually match from a sanitized query."""
    return [part for part in query.split(" OR ") if part]


def _is_cyrillic(term: str) -> bool:
    return any("Ѐ" <= char <= "ӿ" for char in term)


def test_document_actually_exceeds_the_cap() -> None:
    """Guard the premise: without truncation there is nothing to prove."""
    assert len(ASCII_NOISE) > TOKEN_CAP
    assert len(set(ASCII_NOISE)) + len(set(CYRILLIC_CONTENT)) > TOKEN_CAP


@pytest.mark.parametrize("helper", HELPERS)
def test_cap_keeps_cyrillic_content_words(helper) -> None:
    """Acceptance metric: the selected set contains the Bulgarian content."""
    terms = _quoted_terms(helper(_mixed_document()))

    missing = [word for word in CYRILLIC_CONTENT if f'"{word}"' not in terms]
    assert not missing, f"cap dropped Cyrillic content words: {missing}"


@pytest.mark.parametrize("helper", HELPERS)
def test_codepoint_order_would_have_dropped_every_cyrillic_token(helper) -> None:
    """Pin the regression: the pre-fix ordering yields a query with zero Cyrillic."""
    document = _mixed_document()

    # Reconstruct the defective selection from the same token universe.
    from db_utils import tokenize_for_similarity

    codepoint_prefix = sorted(tokenize_for_similarity(document))[:TOKEN_CAP]
    assert not [term for term in codepoint_prefix if _is_cyrillic(term)]

    fixed = [term for term in _quoted_terms(helper(document)) if _is_cyrillic(term)]
    assert len(fixed) == len(CYRILLIC_CONTENT)


@pytest.mark.parametrize("helper", HELPERS)
def test_selection_is_longest_first_then_alphabetical(helper) -> None:
    """Ties break alphabetically, so the emitted query is deterministic."""
    terms = [term.strip('"') for term in _quoted_terms(helper(_mixed_document()))]

    keys = [(-len(term), term) for term in terms]
    assert keys == sorted(keys)
    # The head of the query must be the longest token in the document, not
    # merely a locally sorted run of same-length noise.
    assert terms[0] == "автоматизация"
    assert len(terms[0]) > len(terms[-1])


@pytest.mark.parametrize("helper", HELPERS)
def test_selection_is_stable_under_input_permutation(helper) -> None:
    """Word order in the source text must not change the query."""
    baseline = helper(_mixed_document())
    for seed in (1, 2, 3, 17):
        assert helper(_mixed_document(seed=seed)) == baseline


@pytest.mark.parametrize("helper", HELPERS)
def test_cap_is_still_enforced(helper) -> None:
    """Reordering must not widen the cap."""
    assert len(_quoted_terms(helper(_mixed_document()))) == TOKEN_CAP


@pytest.mark.parametrize("helper", HELPERS)
def test_pure_ascii_documents_are_unaffected_in_kind(helper) -> None:
    """A Latin corpus still yields its long content words, not its noise."""
    document = " ".join([*ASCII_NOISE, "authentication", "reconciliation", "throughput"])
    terms = _quoted_terms(helper(document))

    for word in ("authentication", "reconciliation", "throughput"):
        assert f'"{word}"' in terms


@pytest.mark.parametrize("helper", HELPERS)
@pytest.mark.parametrize("text", ["", "   ", "a b c"])
def test_empty_and_stopword_only_text_yields_no_query(helper, text: str) -> None:
    """Sub-token input must stay falsy so callers skip the MATCH entirely."""
    assert helper(text) == ""


@pytest.mark.parametrize("helper", HELPERS)
def test_query_is_valid_fts5_and_matches_the_cyrillic_row(helper) -> None:
    """End-to-end on a throwaway in-memory index mirroring the real tokenizer."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE probe USING fts5("
            "    body, tokenize = \"unicode61 remove_diacritics 2\")"
        )
        conn.execute("INSERT INTO probe(body) VALUES (?)", (" ".join(CYRILLIC_CONTENT),))
        conn.execute("INSERT INTO probe(body) VALUES ('unrelated latin row')")
        conn.commit()

        query = helper(_mixed_document())
        rows = conn.execute(
            "SELECT rowid FROM probe WHERE probe MATCH ? ORDER BY rank", (query,)
        ).fetchall()

        assert [row[0] for row in rows] == [1]
    finally:
        conn.close()


@pytest.mark.parametrize("helper", HELPERS)
def test_uppercase_cyrillic_survives_the_cap(helper) -> None:
    """Casefolding happens in the tokenizer; the cap must not undo it."""
    document = " ".join([*ASCII_NOISE, *(word.upper() for word in CYRILLIC_CONTENT)])
    terms = _quoted_terms(helper(document))

    for word in CYRILLIC_CONTENT:
        assert f'"{word}"' in terms


def test_both_helpers_implement_the_same_cap_policy() -> None:
    """The two copies of this helper must never disagree."""
    for seed in (0, 5, 11):
        document = _mixed_document(seed=seed)
        assert _safe_fts_query(document) == _safe_task_fts_query(document)
