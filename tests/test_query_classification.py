"""S4 — query classification and telemetry, with suppression deliberately absent.

The measured defect this addresses: three of four sampled queries returned
zero semantically relevant results while still reporting confident matches
(13/40 relevant overall). Root cause is an OR join with no notion of "nothing
here is worth returning".

The fix stops short of suppression on purpose. Coverage and jump sat 0.3% from
flipping on a hold-out set, and one of them inverts for short proper-noun
queries — a strong jump means a *good* match there. Hiding a real answer costs
more than showing a weak one, so the signals label the response and never trim
it. That direction is the opposite of the one chosen for tokenizer parity, and
deliberately so: the cheaper error differs per gate.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from query_classification import (  # noqa: E402
    STOPWORD_LIST_VERSION,
    STOPWORDS,
    classify_query,
    is_year_or_date,
    meaningful_terms,
)


class TestYearAndDateRule:
    """Narrow, not `isdigit()`: identifiers must survive."""

    @pytest.mark.parametrize("token", ["2026", "1999", "2026-08-14", "01.01.2026"])
    def test_years_and_dates_are_not_meaningful(self, token):
        assert is_year_or_date(token) is True

    @pytest.mark.parametrize("token", ["5011", "501", "123", "9999", "42"])
    def test_identifiers_survive(self, token):
        assert is_year_or_date(token) is False, (
            "account and task numbers carry meaning; isdigit() would kill them"
        )

    def test_single_letters_survive(self):
        """`C` is a language, a drive, a grade. Length is not meaning."""
        assert meaningful_terms("C") == ["C"]


class TestStopwords:
    @pytest.mark.parametrize("token", ["the", "and", "for", "with", "is"])
    def test_english_function_words(self, token):
        assert token in STOPWORDS

    @pytest.mark.parametrize("token", ["за", "на", "в", "и", "да", "не"])
    def test_bulgarian_function_words(self, token):
        assert token in STOPWORDS

    @pytest.mark.parametrize(
        "token",
        [
            "mapping",
            "studio",
            "kreston",
            "крестън",
            "мапинг",
            "одит",
            "баланс",
            "сметка",
        ],
    )
    def test_domain_terms_stay_free(self, token):
        """Corpus-common is not the same as meaningless.

        `mapping` covers 30.5% of this corpus and `2026` covers 29.6% — one
        percentage point apart, one central and one noise. No frequency
        threshold separates them, which is why the list is explicit and why
        document frequency never decides on its own.
        """
        assert token not in STOPWORDS

    def test_inflection_is_a_known_v1_limitation(self):
        """Bulgarian inflects; a flat word list does not.

        `които` is listed, `който` is not. Rather than paper over it, the gap
        is asserted so v2's morphology layer arrives as a visible change.
        """
        assert "които" in STOPWORDS
        assert "който" not in STOPWORDS


class TestMeaningfulTerms:
    def test_strips_stopwords_and_years(self):
        assert meaningful_terms("Google Calendar МОРЕ 2026 Семейни") == [
            "Google",
            "Calendar",
            "МОРЕ",
            "Семейни",
        ]

    def test_keeps_domain_terms(self):
        assert meaningful_terms("mapping studio за клиента") == [
            "mapping",
            "studio",
            "клиента",
        ]

    def test_all_stopwords_yields_nothing(self):
        assert meaningful_terms("за на и с от") == []


class TestClassification:
    def test_no_meaningful_terms_is_ambiguous(self):
        assert classify_query("за на и").status == "AMBIGUOUS_QUERY"

    def test_year_only_query_is_ambiguous(self):
        assert classify_query("2026").status == "AMBIGUOUS_QUERY"

    def test_single_token_skips_the_jump_rule(self):
        """A good one-word query *should* have a standout top hit.

        Applying the jump rule here would reject `Крестън` (24% jump), which
        is exactly the query most likely to be meant.
        """
        result = classify_query("Крестън")
        assert result.applies_jump is False
        assert result.status == "OK"

    def test_multi_token_query_is_ok_by_default(self):
        result = classify_query("Мапинг Студио тестване")
        assert result.status == "OK"
        assert result.applies_jump is True


class TestTelemetryNeverSuppresses:
    """The whole point: signals label, they do not trim."""

    @pytest.mark.parametrize(
        "query",
        [
            "Google Calendar МОРЕ 2026 Семейни",
            "job search curator инструкция",
            "READINGS LIST CURATOR Структуриран промпт",
        ],
    )
    def test_the_three_measured_noise_queries_are_not_suppressed(self, query):
        """These scored 0/15, 0/5 and 0/5 on blind labelling.

        They are still not suppressed. The constants that would catch them are
        uncalibrated, and a wrong suppression hides a real answer.
        """
        result = classify_query(query)
        assert result.status != "NO_RELEVANT_RESULTS"
        assert result.suppress is False

    def test_no_status_value_means_suppression(self):
        for query in ("за на", "2026", "Крестън", "Мапинг Студио"):
            assert classify_query(query).suppress is False

    def test_status_values_are_enumerated(self):
        allowed = {"OK", "AMBIGUOUS_QUERY"}
        for query in ("за на", "2026", "Крестън", "Мапинг Студио тест"):
            assert classify_query(query).status in allowed


class TestWiredIntoSearchNodes:
    """The label must reach the caller, and must not shrink the result set."""

    @pytest.fixture()
    def corpus(self):
        import server as S

        getattr(S.create_entities, "fn", S.create_entities)(
            [
                {
                    "name": "Мапинг Студио бележка",
                    "entityType": "note",
                    "observations": ["Мапинг Студио тестване през 2026 година"],
                }
            ]
        )
        return S

    def test_query_status_is_separate_from_result_status(self, corpus):
        """'your question was vague' must not read as 'the payload is broken'."""
        import json

        S = corpus
        acct = json.loads(getattr(S.search_nodes, "fn")("Мапинг Студио"))["_accounting"]
        assert acct["query_status"] in {"OK", "AMBIGUOUS_QUERY"}
        assert acct["status"] in {
            "OK",
            "DEGRADED",
            "UNCALIBRATED",
            "NO_RESULTS_IN_GRAPH",
        }
        assert "meaningful_terms" in acct
        assert acct["stopword_list"] == STOPWORD_LIST_VERSION

    def test_stopword_only_query_is_labelled_but_still_searched(
        self, corpus, monkeypatch
    ):
        """AMBIGUOUS is a label on the question, not a refusal to answer."""
        import json

        S = corpus
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        payload = json.loads(getattr(S.search_nodes, "fn")("за на и"))
        assert payload["_accounting"]["query_status"] == "AMBIGUOUS_QUERY"
        # No assertion that entities is empty: the search still runs. Whether
        # it finds anything is the corpus's business, not the classifier's.
        assert "entities" in payload
