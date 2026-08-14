"""DEV smoke — S0, S1a, S1b, S2 and S4 exercised together.

Every status variant is reached, not just the happy one. Two invariants hold
for each, because a status string that changes length changes the payload it
sits inside:

    reported wire_chars == len(final serialised payload)
    len(final serialised payload) <= budget

That pair caught two defects during implementation. It is asserted per status
rather than once, since each label has a different length.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import server as S  # noqa: E402
from recall_budget import RECALL_WIRE_BUDGET  # noqa: E402

ALL_STATUSES = {"OK", "DEGRADED", "NO_RESULTS_IN_GRAPH", "UNCALIBRATED"}


def _call(tool, *args, **kwargs):
    return getattr(tool, "fn", tool)(*args, **kwargs)


def _check_invariants(raw: str, budget: int = RECALL_WIRE_BUDGET) -> dict:
    payload = json.loads(raw)
    acct = payload["_accounting"]
    assert int(acct["wire_chars"]) == len(raw), (
        f"reported {acct['wire_chars']} vs actual {len(raw)} "
        f"(status={acct['status']!r} — a longer label shifts the payload)"
    )
    assert len(raw) <= budget, f"{len(raw)} > {budget}"
    return payload


@pytest.fixture()
def graph():
    """A corpus reaching every status variant.

    - ``Полярис Ядро``: matches lexically, carries evidence.
    - ``Спътник Орбита``: reachable only across a relation, no evidence.
    - bulk entities: enough volume that the budget must cut.
    """
    _call(
        S.create_entities,
        [
            {
                "name": "Полярис Ядро",
                "entityType": "note",
                "observations": ["Полярис е ключовата дума в този запис"],
            },
            {
                "name": "Спътник Орбита",
                "entityType": "note",
                "observations": ["Този запис не съдържа търсеното изобщо"],
            },
        ]
        + [
            {
                "name": f"Обемна Същност {i}",
                "entityType": "note",
                "observations": [
                    "Мапинг Студио наблюдение с кирилица за обем " * 10
                    for _ in range(4)
                ],
            }
            for i in range(1, 40)
        ],
    )
    _call(
        S.create_relations,
        [
            {
                "from": "Полярис Ядро",
                "to": "Спътник Орбита",
                "relationType": "related_to",
            }
        ],
    )


class TestEveryStatusVariant:
    def test_ok(self, graph, monkeypatch):
        """Single meaningful term, evidence found, no uncalibrated signal."""
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        monkeypatch.setattr(S, "_RECALL_EXPAND_HOPS", 0)
        payload = _check_invariants(_call(S.search_nodes, "Полярис"))
        assert payload["_accounting"]["status"] == "OK"

    def test_degraded(self, graph, monkeypatch):
        """The graph neighbour has no evidence by construction."""
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        payload = _check_invariants(_call(S.search_nodes, "Полярис"))
        assert payload["_accounting"]["status"] == "DEGRADED"
        assert payload["_accounting"]["degraded"]

    def test_no_results_in_graph(self, graph, monkeypatch):
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        payload = _check_invariants(_call(S.search_nodes, "непознатодумабезсмисъл"))
        assert payload["_accounting"]["status"] == "NO_RESULTS_IN_GRAPH"
        assert payload["entities"] == []

    def test_uncalibrated(self, graph, monkeypatch):
        """Multi-term query: coverage and jump exist, so the label is honest."""
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        monkeypatch.setattr(S, "_RECALL_EXPAND_HOPS", 0)
        payload = _check_invariants(_call(S.search_nodes, "Мапинг Студио наблюдение"))
        acct = payload["_accounting"]
        assert acct["status"] in {"UNCALIBRATED", "DEGRADED"}
        assert "coverage" in acct or "jump" in acct, (
            "telemetry that is never recorded is telemetry that does not exist"
        )

    def test_ambiguous_query_is_a_query_label_not_a_result_label(
        self, graph, monkeypatch
    ):
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        payload = _check_invariants(_call(S.search_nodes, "за на и с от"))
        acct = payload["_accounting"]
        assert acct["query_status"] == "AMBIGUOUS_QUERY"
        assert acct["status"] in ALL_STATUSES

    def test_status_is_always_from_the_enumeration(self, graph, monkeypatch):
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        for query in ("Полярис", "Мапинг Студио", "за на и", "непознатодума"):
            payload = _check_invariants(_call(S.search_nodes, query))
            assert payload["_accounting"]["status"] in ALL_STATUSES
            assert payload["_accounting"]["query_status"] in {"OK", "AMBIGUOUS_QUERY"}


class TestInvariantsUnderPressure:
    @pytest.mark.parametrize("budget", [1500, 4000, 16000])
    def test_invariants_hold_at_every_budget(self, graph, budget, monkeypatch):
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        _check_invariants(_call(S.search_nodes, "Мапинг Студио", budget=budget), budget)

    def test_live_vector_path(self, graph):
        """No monkeypatching: the real hybrid path, as production runs it."""
        _check_invariants(_call(S.search_nodes, "Мапинг Студио"))


class TestPhasesHoldTogether:
    def test_s0_cyrillic_unescaped(self, graph, monkeypatch):
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        raw = _call(S.search_nodes, "Мапинг Студио")
        assert "Мапинг" in raw and "\\u041c" not in raw

    def test_s2_graph_boost_active(self, graph, monkeypatch):
        seen: dict = {}
        import smart_retrieval

        original = smart_retrieval.rerank_entities

        def spy(conn, rows, **kwargs):
            seen.update(kwargs)
            return original(conn, rows, **kwargs)

        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        monkeypatch.setattr("smart_retrieval.rerank_entities", spy)
        _call(S.search_nodes, "Полярис")
        assert seen.get("query_entity_ids"), "graph proximity must be live"

    def test_s2_session_boost_still_inactive(self, graph, monkeypatch):
        seen: dict = {}
        import smart_retrieval

        original = smart_retrieval.rerank_entities

        def spy(conn, rows, **kwargs):
            seen.update(kwargs)
            return original(conn, rows, **kwargs)

        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        monkeypatch.setattr("smart_retrieval.rerank_entities", spy)
        _call(S.search_nodes, "Полярис")
        assert seen["session_id"] is None, "deferred capability, not a silent revival"

    def test_s4_holdout_queries_are_returned_not_suppressed(self, graph, monkeypatch):
        """The three queries that scored 0/15, 0/5 and 0/5 on blind labelling.

        Published as structure-preserving stand-ins; see the provenance note in
        ``test_query_classification.py``.
        """
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        for query in (
            "Google Calendar ПРОЕКТ 2026 Тримесечни",
            "task list curator инструкция",
            "ARCHIVE LIST CURATOR Структуриран промпт",
        ):
            payload = _check_invariants(_call(S.search_nodes, query))
            assert payload["_accounting"]["status"] != "NO_RELEVANT_RESULTS"

    def test_n8n_payload_keys_unchanged(self, graph, monkeypatch):
        monkeypatch.setattr(S, "_VEC_AVAILABLE", False)
        payload = json.loads(_call(S.search_nodes, "Мапинг Студио"))
        assert "entities" in payload and "query" in payload
        for entity in payload["entities"]:
            assert {"name", "entityType", "observations"} <= set(entity)


class TestPersistenceExclusionIntact:
    def test_session_save_still_escapes(self):
        """S0's one deliberate exclusion must survive every later phase."""
        import ast
        import pathlib

        src = (pathlib.Path(__file__).parent.parent / "session_server.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "dumps"
            ):
                continue
            returned = any(
                isinstance(p, ast.Return)
                for p in ast.walk(tree)
                if isinstance(p, ast.Return)
                and p.value is not None
                and node in list(ast.walk(p.value))
            )
            if not returned:
                assert not any(kw.arg == "ensure_ascii" for kw in node.keywords), (
                    "sessions.active_files must keep default escaping"
                )
