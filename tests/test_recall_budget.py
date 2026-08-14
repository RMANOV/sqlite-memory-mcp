"""S1a — recall budget primitive.

Boundaries under test:

1. hard wire cap enforced before the payload leaves
2. FTS schema probe copies the real tokenizer, never re-implements it
3. evidence-first selection
4. DEGRADED when evidence is genuinely absent
5. coverage/jump are telemetry — they never suppress results
6. the primitive stays free of db_utils / server imports

The budget unit is **wire characters** of the serialised JSON, never bytes
and never a mix. Every measurement behind the frozen 16000 constant was taken
in characters; switching units would invalidate it without new evidence.
``test_budget_unit_is_characters_not_bytes`` pins that choice so it cannot
drift silently.
"""

import copy
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from recall_budget import (  # noqa: E402
    RECALL_WIRE_BUDGET,
    ProbeSchemaError,
    evidence_ids,
    fts_probe_spec,
    pack_entities,
    window_around,
)

TOKENIZER = "unicode61 remove_diacritics 2"
CYR = "Мапинг Студио наблюдение с кирилица "


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        f'name, entity_type, observations_text, tokenize = "{TOKENIZER}")'
    )
    return c


def entities(n, obs_each=6, text=CYR, size=8):
    return [
        {
            "id": i,
            "name": f"Същност {i}",
            "entityType": "note",
            "observations": [
                {"id": i * 100 + j, "content": text * size} for j in range(obs_each)
            ],
        }
        for i in range(1, n + 1)
    ]


# ── Boundary 6: the primitive must not reach back into the servers ─────────


class TestIsolation:
    def test_imports_nothing_from_db_utils_or_servers(self):
        """Checked on the import graph, not the prose — comments mention both."""
        import ast

        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "recall_budget.py"
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {
            "db_utils",
            "server",
            "task_server",
            "session_server",
            "entity_server",
        }
        assert not (imported & forbidden), (
            f"unexpected dependency: {imported & forbidden}"
        )


# ── Boundary 2: probe mirrors the real schema ──────────────────────────────


class TestProbeSpec:
    def test_reads_tokenizer_from_sqlite_master(self, conn):
        spec = fts_probe_spec(conn, "memory_fts")
        assert spec.tokenize == TOKENIZER
        assert spec.columns == ["name", "entity_type", "observations_text"]

    def test_rejects_unknown_tokenizer(self, conn):
        conn.execute('CREATE VIRTUAL TABLE odd USING fts5(body, tokenize = "porter")')
        with pytest.raises(ProbeSchemaError):
            fts_probe_spec(conn, "odd")

    def test_rejects_external_content_table(self, conn):
        conn.execute("CREATE TABLE src(id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute(
            "CREATE VIRTUAL TABLE ext USING fts5(body, content='src', content_rowid='id')"
        )
        with pytest.raises(ProbeSchemaError):
            fts_probe_spec(conn, "ext")

    def test_missing_table_fails_closed(self, conn):
        with pytest.raises(ProbeSchemaError):
            fts_probe_spec(conn, "no_such_table")


class TestEvidenceProbe:
    """Parity is identity: the probe uses the tokenizer, never mimics it."""

    def test_cyrillic_breve_matches_index_semantics(self, conn):
        spec = fts_probe_spec(conn, "memory_fts")
        rows = [(1, "който край май"), (2, "нищо общо")]
        assert evidence_ids(rows, '"който"', spec) == {1}
        # 'и'+U+0306 is a distinct token; NFD folding would wrongly match here
        assert evidence_ids(rows, '"които"', spec) == set()

    def test_latin_diacritics_follow_the_tokenizer(self, conn):
        spec = fts_probe_spec(conn, "memory_fts")
        assert evidence_ids([(1, "résumé du client")], '"resume"', spec) == {1}

    def test_ligature_is_not_decomposed(self, conn):
        """NFKD folds ﬁ -> fi; unicode61 does not. The probe must agree."""
        spec = fts_probe_spec(conn, "memory_fts")
        assert evidence_ids([(1, "ﬁle ﬂow")], '"file"', spec) == set()

    def test_empty_query_returns_nothing(self, conn):
        spec = fts_probe_spec(conn, "memory_fts")
        assert evidence_ids([(1, "текст")], "", spec) == set()


# ── Windowing ──────────────────────────────────────────────────────────────


class TestWindow:
    def test_returns_text_untouched_when_short(self):
        assert window_around("кратко", [0], 600) == "кратко"

    def test_centres_on_the_match_not_the_head(self):
        text = "X" * 3000 + " полярис " + "Y" * 3000
        out = window_around(text, [3001], 600)
        assert "полярис" in out

    def test_marks_truncation_visibly(self):
        assert window_around("Z" * 5000, [0], 600).endswith("…")

    def test_no_position_falls_back_to_head(self):
        out = window_around("A" * 5000, [], 600)
        assert out.startswith("A")


# ── Boundary 1: the cap ────────────────────────────────────────────────────


class TestBudgetCap:
    def test_budget_unit_is_characters_not_bytes(self):
        """Cyrillic is 2 bytes/char in UTF-8; the accounting must not drift."""
        out, acct = pack_entities(entities(20), evidence=set(), query_terms=["мапинг"])
        payload = json.dumps({"entities": out, "_accounting": acct}, ensure_ascii=False)
        assert int(acct["wire_chars"]) == len(payload)
        assert int(acct["wire_chars"]) < len(payload.encode("utf-8"))
        # fixed width: the field sits inside the payload it measures
        assert len(acct["wire_chars"]) == 7

    def test_under_cap_is_not_truncated(self):
        out, acct = pack_entities(
            entities(2, obs_each=1, size=1), evidence=set(), query_terms=["x"]
        )
        assert acct["truncated"] is False
        assert acct["entities_returned"] == 2
        assert int(acct["wire_chars"]) < RECALL_WIRE_BUDGET

    def test_over_cap_is_truncated_and_within_budget(self):
        out, acct = pack_entities(
            entities(60), evidence=set(), query_terms=["x"], budget=4000
        )
        assert acct["truncated"] is True
        assert int(acct["wire_chars"]) <= 4000
        assert 0 < acct["entities_returned"] < 60

    def test_default_budget_is_the_frozen_constant(self):
        _, acct = pack_entities(entities(60), evidence=set(), query_terms=["x"])
        assert acct["budget_wire_chars"] == RECALL_WIRE_BUDGET
        assert int(acct["wire_chars"]) <= RECALL_WIRE_BUDGET

    def test_entity_count_is_an_outcome_not_a_constant(self):
        _, small = pack_entities(
            entities(60), evidence=set(), query_terms=["x"], budget=3000
        )
        _, large = pack_entities(
            entities(60), evidence=set(), query_terms=["x"], budget=15000
        )
        assert large["entities_returned"] > small["entities_returned"]

    def test_single_oversized_record_is_returned_not_dropped(self):
        """One entity bigger than the whole budget must still come back.

        Returning nothing would hide the only answer; the cap yields rather
        than producing an empty result.
        """
        huge = entities(1, obs_each=1, size=4000)
        out, acct = pack_entities(huge, evidence=set(), query_terms=["x"], budget=500)
        assert len(out) == 1
        assert acct["entities_returned"] == 1
        assert acct["truncated"] is False  # nothing was left behind

    def test_accounting_reports_the_full_candidate_pool(self):
        _, acct = pack_entities(
            entities(60), evidence=set(), query_terms=["x"], budget=3000
        )
        assert acct["entities_considered"] == 60
        assert acct["entities_returned"] < 60


# ── Output shape ───────────────────────────────────────────────────────────


class TestOutputShape:
    def test_payload_is_valid_json_and_round_trips(self):
        out, acct = pack_entities(entities(20), evidence=set(), query_terms=["мапинг"])
        payload = json.dumps({"entities": out, "_accounting": acct}, ensure_ascii=False)
        assert json.loads(payload)["entities"] == out

    def test_truncation_happens_on_text_never_on_serialised_json(self):
        """Slicing serialised JSON would produce invalid output."""
        out, _ = pack_entities(
            entities(40), evidence=set(), query_terms=["x"], budget=2000
        )
        for entity in out:
            for text in entity["observations"]:
                assert isinstance(text, str)
        json.loads(json.dumps({"entities": out}, ensure_ascii=False))

    def test_cyrillic_survives_unescaped(self):
        out, _ = pack_entities(entities(3), evidence=set(), query_terms=["мапинг"])
        payload = json.dumps({"entities": out}, ensure_ascii=False)
        assert "Мапинг" in payload
        assert "\\u041c" not in payload

    def test_deterministic_for_identical_input(self):
        a_out, a_acct = pack_entities(
            entities(30), evidence=set(), query_terms=["x"], budget=6000
        )
        b_out, b_acct = pack_entities(
            entities(30), evidence=set(), query_terms=["x"], budget=6000
        )
        assert a_out == b_out
        assert a_acct == b_acct

    def test_does_not_mutate_its_input(self):
        source = entities(10)
        snapshot = copy.deepcopy(source)
        pack_entities(source, evidence=set(), query_terms=["x"], budget=3000)
        assert source == snapshot


# ── Boundaries 3 and 4: evidence-first and DEGRADED ────────────────────────


class TestEvidenceFirst:
    def test_evidence_observation_takes_slot_one(self):
        ents = entities(2, obs_each=4)
        evidence = {e["observations"][-1]["id"] for e in ents}
        out, _ = pack_entities(ents, evidence=evidence, query_terms=["x"])
        for entity in out:
            assert entity["_evidence_status"] == "found"
            assert entity["observations"]

    def test_missing_evidence_is_reported_not_hidden(self):
        out, acct = pack_entities(entities(3), evidence=set(), query_terms=["x"])
        assert acct["status"] == "DEGRADED"
        assert all(e["_evidence_status"] == "not_found" for e in out)
        assert len(acct["degraded"]) == len(out)

    def test_status_ok_when_all_entities_have_evidence(self):
        ents = entities(3)
        evidence = {o["id"] for e in ents for o in e["observations"]}
        _, acct = pack_entities(ents, evidence=evidence, query_terms=["x"])
        assert acct["status"] == "OK"
        assert acct["degraded"] == []


# ── Boundary 5: telemetry never suppresses ─────────────────────────────────


class TestTelemetryNeverSuppresses:
    def test_low_coverage_still_returns_entities(self):
        out, acct = pack_entities(
            entities(4), evidence=set(), query_terms=["a"], coverage=0.10, jump=0.90
        )
        assert out, "telemetry must never empty the result set"
        assert acct["status"] != "NO_RELEVANT_RESULTS"

    def test_signals_are_recorded_when_supplied(self):
        ents = entities(2)
        evidence = {o["id"] for e in ents for o in e["observations"]}
        _, acct = pack_entities(
            ents, evidence=evidence, query_terms=["a"], coverage=0.25, jump=0.42
        )
        assert acct["coverage"] == 0.25
        assert acct["jump"] == 0.42
        assert acct["status"] == "UNCALIBRATED"
