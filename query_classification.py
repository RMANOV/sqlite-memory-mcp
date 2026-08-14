"""S4 — classify a query's tokens; label the response, never trim it.

Blind labelling of 40 sampled results found 13 relevant. Three of four queries
returned nothing relevant at all while reporting confident matches, because
``fts_query`` joins terms with OR: one common token (``2026``, ``search``,
``list``) drags in the whole corpus. AND is not the fix — it returned zero
results for all six queries tested, since no entity contains every word of a
natural-language question.

What is missing is a way to say "nothing here is worth returning". This module
provides the part of that which is safe to act on, and stops before the part
that is not:

**Acted on** — an explicit, versioned stopword list plus a narrow year/date
rule. Both are deterministic and their failure mode is visible.

**Not acted on** — coverage and jump. On a hold-out set their thresholds sat
0.3% from flipping, with two queries landing exactly on 33.0%; and the jump
signal *inverts* for short proper-noun queries, where a large gap means the
top hit genuinely stands out. They are recorded as telemetry.

The direction here is the opposite of the one taken for tokenizer parity, on
purpose. There, the costlier error was claiming false evidence, so the narrower
option won. Here, the costlier error is hiding a real answer, so the wider one
does. Fail-safe is not a fixed direction — it follows whichever mistake hurts
more at that gate.

Document frequency deliberately does not decide anything. ``mapping`` covers
30.5% of this corpus and ``2026`` covers 29.6%: one is the subject of almost
everything, the other is noise, and they are one percentage point apart.
Telling them apart needs an external reference corpus, which does not exist
here — hence an explicit list rather than a frequency cut.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STOPWORD_LIST_VERSION = "v1"

# Function words only. Domain vocabulary is deliberately absent: corpus-common
# and meaningless are different things, and no frequency threshold separates
# them here.
_BG_STOPWORDS = {
    "и",
    "а",
    "но",
    "или",
    "да",
    "не",
    "ще",
    "са",
    "е",
    "бе",
    "съм",
    "си",
    "се",
    "го",
    "я",
    "ги",
    "на",
    "в",
    "във",
    "с",
    "със",
    "за",
    "от",
    "до",
    "по",
    "при",
    "над",
    "под",
    "през",
    "без",
    "към",
    "около",
    "този",
    "тази",
    "това",
    "тези",
    "той",
    "тя",
    "то",
    "те",
    "аз",
    "ти",
    "ние",
    "вие",
    "кой",
    "коя",
    "кое",
    "които",
    "как",
    "кога",
    "къде",
    "защо",
    "като",
    "ако",
    "че",
    "още",
    "вече",
    "само",
    "също",
    "много",
    "повече",
}

_EN_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "if",
    "of",
    "in",
    "on",
    "at",
    "to",
    "for",
    "with",
    "from",
    "by",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "has",
    "have",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "he",
    "she",
    "they",
    "we",
    "you",
    "i",
    "not",
    "no",
    "yes",
    "can",
    "could",
    "should",
    "may",
    "might",
    "all",
    "any",
    "some",
    "more",
    "most",
    "only",
    "also",
    "very",
}

STOPWORDS = frozenset(_BG_STOPWORDS | _EN_STOPWORDS)

# Years and dates are matched by *rule*, never enumerated: a list of years
# grows without end. The window is narrow on purpose — `isdigit()` would also
# reject 5011, 501 and 123, which are account and task identifiers carrying
# real meaning, and `len(t) < 2` would reject `C`.
_YEAR_MIN, _YEAR_MAX = 1900, 2100
_DATE_RE = re.compile(r"\d{1,4}[-./]\d{1,2}([-./]\d{1,4})?$")
_TOKEN_RE = re.compile(r"[^\wЀ-ӿ.-]+")


def is_year_or_date(token: str) -> bool:
    if _DATE_RE.match(token):
        return True
    if token.isdigit() and len(token) == 4:
        return _YEAR_MIN <= int(token) <= _YEAR_MAX
    return False


def tokenize(raw: str) -> list[str]:
    return [t for t in _TOKEN_RE.split(raw) if t]


def meaningful_terms(raw: str) -> list[str]:
    """Tokens that could plausibly carry the query's intent."""
    return [
        t
        for t in tokenize(raw)
        if t.lower() not in STOPWORDS and not is_year_or_date(t)
    ]


@dataclass(frozen=True)
class QueryClass:
    status: str
    terms: list[str]
    applies_jump: bool
    suppress: bool = False  # structurally always False; see module docstring
    reason: str = ""


def classify_query(raw: str) -> QueryClass:
    """Label the query. Never decide that results should be withheld.

    ``suppress`` is part of the contract precisely so that its value is
    checkable, and it is always ``False``: a caller cannot accidentally start
    trimming without changing this function and its tests together.
    """
    terms = meaningful_terms(raw)

    if not terms:
        return QueryClass(
            status="AMBIGUOUS_QUERY",
            terms=[],
            applies_jump=False,
            reason="query is only stopwords, years or dates",
        )

    if len(terms) == 1:
        # The jump rule inverts for a single meaningful token: a good one-word
        # query *should* have a standout top hit. Applying it would reject
        # `Крестън` (24% jump), the very query most likely to be intended.
        return QueryClass(
            status="OK",
            terms=terms,
            applies_jump=False,
            reason="single meaningful term; jump rule does not apply",
        )

    return QueryClass(status="OK", terms=terms, applies_jump=True)
