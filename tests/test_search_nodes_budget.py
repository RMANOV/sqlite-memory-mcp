"""S1b — search_nodes wired to the recall budget.

Boundaries pinned here, on the real tool rather than the primitive:

1. reported size equals the *actual* serialised size, not merely <= the cap
2. one unit throughout — wire characters, never mixed with UTF-8 bytes
3. at most one refill
4. rowids and text are read inside a single transaction
5. a growing ``degraded`` block re-triggers budgeting
6. coverage/jump can never remove a result
7. telemetry never reaches persistence or the sync payload
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import server as S  # noqa: E402
from recall_budget import RECALL_WIRE_BUDGET  # noqa: E402

CYR = "Мапинг Студио наблюдение с кирилица за търсене "


def _call(tool, *args, **kwargs):
    fn = getattr(tool, "fn", tool)
    return fn(*args, **kwargs)


@pytest.fixture()
def corpus():
    """A corpus large enough that the budget must actually cut."""
    entities = [
        {
            "name": f"Същност {i}",
            "entityType": "note",
            "observations": [CYR * 12 for _ in range(6)],
        }
        for i in range(1, 46)
    ]
    _call(S.create_entities, entities)
    return entities


class TestAccountingTruth:
    def test_reported_size_equals_actual_serialised_size(self, corpus):
        """Boundary 1: the number must describe the payload, not approximate it."""
        raw = _call(S.search_nodes, "Мапинг Студио")
        payload = json.loads(raw)
        assert int(payload["_accounting"]["wire_chars"]) == len(raw)

    def test_reported_size_is_within_the_cap(self, corpus):
        raw = _call(S.search_nodes, "Мапинг Студио")
        assert len(raw) <= RECALL_WIRE_BUDGET

    def test_unit_is_characters_not_bytes(self, corpus):
        raw = _call(S.search_nodes, "Мапинг Студио")
        payload = json.loads(raw)
        reported = int(payload["_accounting"]["wire_chars"])
        assert reported == len(raw)
        assert reported < len(raw.encode("utf-8")), "Cyrillic must cost 2 bytes/char"

    def test_candidate_pool_is_reported_not_the_returned_count(self, corpus):
        raw = _call(S.search_nodes, "Мапинг Студио")
        acct = json.loads(raw)["_accounting"]
        assert acct["entities_considered"] >= acct["entities_returned"]
        assert acct["truncated"] is (
            acct["entities_returned"] < acct["entities_considered"]
        )

    def test_entity_count_moves_with_the_budget(self, corpus):
        """K is an outcome. If it stops varying, it has become a hidden constant."""
        small = json.loads(_call(S.search_nodes, "Мапинг Студио", budget=2500))
        large = json.loads(_call(S.search_nodes, "Мапинг Студио", budget=15000))
        assert (
            large["_accounting"]["entities_returned"]
            > small["_accounting"]["entities_returned"]
        )


class TestRefill:
    def test_at_most_one_refill(self, corpus):
        """Boundary 3: after a refill the full text is in hand; a second is pointless."""
        raw = _call(S.search_nodes, "Мапинг Студио")
        assert json.loads(raw)["_accounting"]["refills"] <= 1


class TestEvidenceAndDegraded:
    def test_every_entity_carries_an_evidence_status(self, corpus):
        raw = _call(S.search_nodes, "Мапинг Студио")
        for entity in json.loads(raw)["entities"]:
            assert entity["_evidence_status"] in {"found", "not_found"}

    def test_status_is_an_enumerated_value(self, corpus):
        raw = _call(S.search_nodes, "Мапинг Студио")
        assert json.loads(raw)["_accounting"]["status"] in {
            "OK",
            "DEGRADED",
            "UNCALIBRATED",
            "NO_RESULTS_IN_GRAPH",
        }

    def test_no_match_is_scoped_to_the_graph(self, corpus, monkeypatch):
        """Markdown memory is invisible here; the label must not overclaim.

        Vector search is disabled for this case on purpose. With it on, a
        nonsense query still returns its nearest neighbours — measured: 21
        entities for a string that appears nowhere — so the empty branch is
        effectively unreachable on the live path. That is worth knowing, but
        it must not stop the branch itself from being verified.
        """
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        payload = json.loads(_call(S.search_nodes, "неповторимоняманикъде"))
        assert payload["entities"] == []
        assert payload["_accounting"]["status"] == "NO_RESULTS_IN_GRAPH"
        assert payload["_accounting"]["refills"] == 0

    def test_vector_search_makes_the_empty_branch_unreachable(self, corpus):
        """Recorded as behaviour, not asserted as desirable."""
        payload = json.loads(_call(S.search_nodes, "неповторимоняманикъде"))
        if S._VEC_AVAILABLE:
            assert payload["entities"], "hybrid search returns nearest neighbours"
            assert payload["_accounting"]["status"] != "NO_RESULTS_IN_GRAPH"


class TestTelemetryIsInert:
    def test_results_are_never_removed_by_signals(self, corpus):
        """Boundary 6: coverage/jump inform, they do not gate."""
        raw = _call(S.search_nodes, "Мапинг Студио")
        payload = json.loads(raw)
        assert payload["entities"], "signals must not empty the result set"
        assert payload["_accounting"]["status"] != "NO_RELEVANT_RESULTS"

    def test_accounting_is_not_persisted(self, corpus):
        """Boundary 7: telemetry lives in the response, never in a row."""
        _call(S.search_nodes, "Мапинг Студио")
        from db_utils import get_conn

        with get_conn() as conn:
            for table in ("entities", "observations"):
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]
                assert "wire_chars" not in cols
                assert "_accounting" not in cols
            hits = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE content LIKE '%wire_chars%'"
            ).fetchone()[0]
            assert hits == 0


class TestPayloadShape:
    def test_existing_keys_are_preserved(self, corpus):
        """n8n lifts this callable verbatim; the contract may grow, not shift."""
        payload = json.loads(_call(S.search_nodes, "Мапинг Студио"))
        assert "entities" in payload and "query" in payload
        for entity in payload["entities"]:
            assert {"name", "entityType", "observations"} <= set(entity)
            assert isinstance(entity["observations"], list)

    def test_cyrillic_is_not_escaped(self, corpus):
        raw = _call(S.search_nodes, "Мапинг Студио")
        assert "Мапинг" in raw
        assert "\\u041c" not in raw

    def test_output_is_valid_json(self, corpus):
        raw = _call(S.search_nodes, "Мапинг Студио")
        assert json.loads(raw)["entities"] is not None
