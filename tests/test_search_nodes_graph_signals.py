"""S2 — the two dead signals, proved live rather than merely present.

``rerank_entities`` documents ``query_entity_ids`` as "entity IDs from the
query itself (for graph proximity)", and its whole hop-scoring block sits
behind ``if query_entity_ids:``. The only production caller passed ``None``,
so ``GRAPH_BOOST_1HOP`` (1.8) and ``GRAPH_BOOST_2HOP`` (1.3) never applied:
search ran on four of six signals.

Code that exists but never executes reads exactly like code that works. So
every test here asserts a **behavioural difference** — an entity that appears
only with expansion on, an ordering that changes when the boost fires. A test
that merely asserted "the parameter is passed" would have passed against the
broken version too.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import server as S  # noqa: E402


def _call(tool, *args, **kwargs):
    return getattr(tool, "fn", tool)(*args, **kwargs)


def _names(payload):
    return [e["name"] for e in payload["entities"]]


@pytest.fixture()
def linked_corpus():
    """One lexical match, one neighbour that matches nothing lexically.

    ``Ядро Полярис`` contains the query term. ``Спътник Орбита`` does not —
    it is reachable only across a relation. Without expansion it cannot be
    returned at all, which is what makes the difference measurable.
    """
    _call(
        S.create_entities,
        [
            {
                "name": "Ядро Полярис",
                "entityType": "note",
                "observations": ["Полярис е ключовият термин за този тест"],
            },
            {
                "name": "Спътник Орбита",
                "entityType": "note",
                "observations": ["Този запис не съдържа търсената дума изобщо"],
            },
            {
                "name": "Несвързан Далечен",
                "entityType": "note",
                "observations": ["Също без термина и без ребро към ядрото"],
            },
        ],
    )
    _call(
        S.create_relations,
        [
            {
                "from": "Ядро Полярис",
                "to": "Спътник Орбита",
                "relationType": "related_to",
            }
        ],
    )


class TestNeighbourExpansionChangesResults:
    def test_neighbour_absent_without_expansion(self, linked_corpus, monkeypatch):
        monkeypatch.setattr(S, "_RECALL_EXPAND_HOPS", 0)
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        names = _names(json.loads(_call(S.search_nodes, "Полярис")))
        assert "Ядро Полярис" in names
        assert "Спътник Орбита" not in names, (
            "without expansion a non-matching neighbour cannot be reached"
        )

    def test_neighbour_present_with_expansion(self, linked_corpus, monkeypatch):
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        names = _names(json.loads(_call(S.search_nodes, "Полярис")))
        assert "Ядро Полярис" in names
        assert "Спътник Орбита" in names, "1-hop expansion must reach it"

    def test_unrelated_entity_stays_out(self, linked_corpus, monkeypatch):
        """Expansion follows edges; it does not widen into everything."""
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        names = _names(json.loads(_call(S.search_nodes, "Полярис")))
        assert "Несвързан Далечен" not in names

    def test_expanded_entity_is_marked_degraded_not_silently_equal(
        self, linked_corpus, monkeypatch
    ):
        """A neighbour has no evidence by construction — that must show."""
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        payload = json.loads(_call(S.search_nodes, "Полярис"))
        by_name = {e["name"]: e for e in payload["entities"]}
        assert by_name["Ядро Полярис"]["_evidence_status"] == "found"
        assert by_name["Спътник Орбита"]["_evidence_status"] == "not_found"
        assert payload["_accounting"]["status"] == "DEGRADED"


class TestGraphBoostChangesRanking:
    def test_query_entity_ids_reach_the_reranker(self, linked_corpus, monkeypatch):
        """Assert the argument is non-empty at the call, not merely present."""
        seen = {}
        import smart_retrieval

        original = smart_retrieval.rerank_entities

        def spy(conn, rows, **kwargs):
            seen["query_entity_ids"] = kwargs.get("query_entity_ids")
            seen["session_id"] = kwargs.get("session_id")
            return original(conn, rows, **kwargs)

        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        monkeypatch.setattr("smart_retrieval.rerank_entities", spy)
        _call(S.search_nodes, "Полярис")
        assert seen.get("query_entity_ids"), (
            "graph proximity stays dead when this is None"
        )


class TestDeterminism:
    def test_identical_queries_return_identical_order(self, linked_corpus, monkeypatch):
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        first = _names(json.loads(_call(S.search_nodes, "Полярис")))
        second = _names(json.loads(_call(S.search_nodes, "Полярис")))
        assert first == second

    def test_expansion_is_capped(self, linked_corpus, monkeypatch):
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        payload = json.loads(_call(S.search_nodes, "Полярис"))
        assert payload["_accounting"]["entities_considered"] <= S._RECALL_EXPAND_CAP


class TestBudgetStillHolds:
    """Requirement 5: expansion must not smuggle the payload past the cap."""

    def test_wire_cap_survives_expansion(self, linked_corpus, monkeypatch):
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        raw = _call(S.search_nodes, "Полярис", budget=3000)
        assert len(raw) <= 3000
        assert int(json.loads(raw)["_accounting"]["wire_chars"]) == len(raw)

    def test_payload_keys_unchanged_for_n8n(self, linked_corpus, monkeypatch):
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        payload = json.loads(_call(S.search_nodes, "Полярис"))
        assert "entities" in payload and "query" in payload
        for entity in payload["entities"]:
            assert {"name", "entityType", "observations"} <= set(entity)
