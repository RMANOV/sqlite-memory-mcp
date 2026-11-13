"""Lifecycle guards and transaction behavior, using disposable databases only."""

import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import claim_graph
import db_utils
import memory_audit
from context_packer import build_context_pack
from schema import init_db


TS = "2026-11-13T06:00:00+00:00"
END = "2026-11-13T07:00:00+00:00"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "lifecycle.db"
    init_db(str(path))
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO tasks (id,title,description,status,priority,section,type,created_at,updated_at) "
        "VALUES ('lifecycle-task','Aurora service configuration','Aurora service configuration',"
        "'not_started','medium','inbox','task',?,?)", (TS, TS)
    )
    yield conn, path
    conn.close()


def fact(fact_id, value=None, **extra):
    return dict(
        fact_id=fact_id, subject="Aurora service", predicate="configuration",
        object_text=value or fact_id, object_type="text", fact_scope="task",
        provenance_summary="synthetic lifecycle evidence", confidence=0.95,
        validation_mode="manual", created_at=TS, updated_at=TS, **extra
    )


def insert(conn, *facts):
    assert db_utils.import_canonical_facts(conn, list(facts)) == len(facts)
    for f in facts:
        conn.execute(
            "INSERT INTO provenance_links (provenance_id,subject_kind,subject_ref,"
            "source_kind,source_ref,excerpt,confidence,created_at) "
            "VALUES (?, 'fact', ?, 'chunk', ?, 'synthetic evidence', 1, ?)",
            ("prov-" + f["fact_id"], f["fact_id"], "source-" + f["fact_id"], TS),
        )


def body(conn):
    return build_context_pack(conn, pack_type="executor", target_ref="lifecycle-task",
                              token_budget=2000, persist=False)["body"]


def fresh_read(path):
    """A new process loads this checkout and reads only the explicit test DB."""
    code = """
import json, pathlib, sqlite3, sys
import context_packer
assert pathlib.Path(context_packer.__file__).resolve().parent == pathlib.Path(sys.argv[2]).resolve()
c=sqlite3.connect(sys.argv[1]); c.row_factory=sqlite3.Row
print(json.dumps({'body':context_packer.build_context_pack(c,pack_type='executor',target_ref='lifecycle-task',token_budget=2000,persist=False)['body'],
 'facts':[dict(r) for r in c.execute("SELECT fact_id,object_text,valid_to,superseded_by_fact_id FROM canonical_facts WHERE fact_id LIKE 'case-%' ORDER BY fact_id")],
 'links':c.execute("SELECT COUNT(*) FROM knowledge_links WHERE subject_ref LIKE 'case-%' OR object_ref LIKE 'case-%'").fetchone()[0]}))
c.close()
"""
    result = subprocess.run([sys.executable, "-c", code, str(path), str(Path(__file__).resolve().parents[1])],
                            check=True, capture_output=True, text=True, timeout=10)
    return json.loads(result.stdout)


@pytest.mark.parametrize("end", [None, ""])
@pytest.mark.parametrize("successor", ["valid", "missing", "cycle", "contested"])
def test_legacy_pointer_only_never_returns_old_fact(database, end, successor):
    conn, path = database
    insert(conn, fact("case-old", "OBSOLETE_SENTINEL"), fact("case-new", "SUCCESSOR_SENTINEL"))
    # Simulates persisted legacy/import corruption, including a dangling FK.
    # This connection is a disposable fixture; no production repair is done.
    conn.execute("PRAGMA foreign_keys=OFF")
    target = "case-missing" if successor == "missing" else "case-new"
    conn.execute("UPDATE canonical_facts SET superseded_by_fact_id=?,valid_to=? WHERE fact_id='case-old'", (target, end))
    if successor == "cycle":
        conn.execute("UPDATE canonical_facts SET superseded_by_fact_id='case-old' WHERE fact_id='case-new'")
    if successor == "contested":
        conn.execute("UPDATE canonical_facts SET contradiction_count=1 WHERE fact_id='case-new'")
    conn.execute("PRAGMA foreign_keys=ON")
    current = body(conn)
    assert "OBSOLETE_SENTINEL" not in current
    assert ("SUCCESSOR_SENTINEL" in current) == (successor in {"valid", "missing"})
    restarted = fresh_read(path)
    assert restarted["body"] == current
    assert restarted["facts"][1]["superseded_by_fact_id"] == target


def test_normal_supersession_invalidation_revalidation_and_contested(database):
    conn, path = database
    insert(conn, fact("case-old", "OBSOLETE_SENTINEL"), fact("case-new", "CURRENT_SENTINEL"))
    assert memory_audit.govern_fact(conn, "case-old", "supersede", target_fact_id="case-new", effective_at=END)["changed"]
    assert "OBSOLETE_SENTINEL" not in body(conn)
    assert "CURRENT_SENTINEL" in body(conn)
    assert fresh_read(path)["facts"][1]["valid_to"] == END
    assert memory_audit.govern_fact(conn, "case-new", "invalidate", effective_at=END)["changed"]
    assert "CURRENT_SENTINEL" not in body(conn)
    assert memory_audit.govern_fact(conn, "case-new", "revalidate")["changed"]
    assert "CURRENT_SENTINEL" in body(conn)
    conn.execute("UPDATE canonical_facts SET contradiction_count=1 WHERE fact_id='case-new'")
    assert "CURRENT_SENTINEL" not in body(conn)


@pytest.mark.parametrize("pointer,end", [(None, None), ("", "")])
def test_empty_lifecycle_fields_do_not_hide_current_fact(database, pointer, end):
    conn, _ = database
    insert(conn, fact("case-current", "CURRENT_SENTINEL"))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("UPDATE canonical_facts SET superseded_by_fact_id=?,valid_to=? WHERE fact_id='case-current'", (pointer, end))
    assert "CURRENT_SENTINEL" in body(conn)


@pytest.mark.parametrize("end", [None, ""])
def test_import_rejects_incomplete_supersession_before_any_fact_write(database, end):
    conn, _ = database
    insert(conn, fact("case-target"))
    payload = [fact("case-first"), fact("case-bad", superseded_by_fact_id="case-target", valid_to=end)]
    original = copy.deepcopy(payload)
    before = conn.total_changes
    with pytest.raises(sqlite3.IntegrityError, match="superseded_by_fact_id.*valid_to"):
        db_utils.import_canonical_facts(conn, payload)
    assert conn.total_changes == before
    assert payload == original
    assert conn.execute("SELECT COUNT(*) FROM canonical_facts WHERE fact_id IN ('case-first','case-bad')").fetchone()[0] == 0


def test_valid_import_then_readout_keeps_only_current(database):
    conn, path = database
    insert(conn, fact("case-new", "CURRENT_SENTINEL"), fact("case-old", "OBSOLETE_SENTINEL",
                                                         superseded_by_fact_id="case-new", valid_to=END))
    assert "OBSOLETE_SENTINEL" not in body(conn)
    assert "CURRENT_SENTINEL" in fresh_read(path)["body"]
    assert conn.execute("SELECT provenance_summary FROM canonical_facts WHERE fact_id='case-old'").fetchone()[0] == "synthetic lifecycle evidence"


@pytest.mark.parametrize("pointer", [0, False])
def test_import_does_not_treat_text_coercible_zero_pointer_as_empty(database, pointer):
    conn, _ = database
    insert(conn, fact("0"))
    with pytest.raises(sqlite3.IntegrityError, match="superseded_by_fact_id.*valid_to"):
        db_utils.import_canonical_facts(conn, [fact("case-bad", superseded_by_fact_id=pointer)])
    assert conn.execute("SELECT COUNT(*) FROM canonical_facts WHERE fact_id='case-bad'").fetchone()[0] == 0


def test_import_late_sql_error_rolls_back_fact_batch_but_not_outer_transaction(database, tmp_path):
    conn, path = database
    insert(conn, fact("case-existing", "ORIGINAL_SENTINEL"))
    good = fact("case-existing", "OVERWRITTEN_SENTINEL")
    good["updated_at"] = END
    bad = fact("case-bad")
    bad["object_text"] = None  # SQL NOT NULL error after the first UPSERT.
    with db_utils.get_conn(str(path)) as transaction:
        transaction.execute("UPDATE tasks SET description='outer change survives' WHERE id='lifecycle-task'")
        result = db_utils.import_remote_bridge_data(transaction, str(tmp_path / "empty-bridge"),
                                                   {"canonical_facts": [good, bad]})
        assert result["facts"] == 0  # Existing caught-sqlite-error API.
        assert transaction.in_transaction
    restored = fresh_read(path)
    assert restored["facts"] == [dict(fact_id="case-existing", object_text="ORIGINAL_SENTINEL",
                                     valid_to=None, superseded_by_fact_id=None)]
    assert conn.execute("SELECT description FROM tasks WHERE id='lifecycle-task'").fetchone()[0] == "outer change survives"


def test_successful_import_stays_inside_caller_rollback(database):
    _, path = database
    with pytest.raises(RuntimeError, match="outer failure"):
        with db_utils.get_conn(str(path)) as transaction:
            assert db_utils.import_canonical_facts(transaction, [fact("case-new")]) == 1
            raise RuntimeError("outer failure")
    assert fresh_read(path)["facts"] == []


@pytest.mark.parametrize("isolation", ["", "IMMEDIATE", "EXCLUSIVE"])
def test_import_preserves_implicit_transaction_ownership(database, isolation):
    _, path = database
    conn = sqlite3.connect(str(path), isolation_level=isolation)
    conn.row_factory = sqlite3.Row
    try:
        assert not conn.in_transaction
        assert db_utils.import_canonical_facts(conn, [fact("case-pending")]) == 1
        assert conn.in_transaction  # The caller, not the importer, owns COMMIT.
        conn.rollback()
    finally:
        conn.close()
    assert fresh_read(path)["facts"] == []


def test_empty_or_unchanged_import_does_not_open_implicit_transaction(database):
    original, path = database
    insert(original, fact("case-existing"))
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        for payload in ([], [fact("case-existing")]):
            assert db_utils.import_canonical_facts(conn, payload) == 0
            assert not conn.in_transaction
    finally:
        conn.close()


@pytest.mark.skipif(not hasattr(sqlite3, "LEGACY_TRANSACTION_CONTROL"), reason="Python <3.12 lacks explicit autocommit")
def test_explicit_autocommit_remains_autocommit(database):
    _, path = database
    conn = sqlite3.connect(str(path), autocommit=True)
    conn.row_factory = sqlite3.Row
    try:
        assert db_utils.import_canonical_facts(conn, [fact("case-committed")]) == 1
        assert not conn.in_transaction
        conn.rollback()
    finally:
        conn.close()
    assert fresh_read(path)["facts"][0]["fact_id"] == "case-committed"


def claim(conn, value):
    conn.execute("INSERT INTO context_chunks (chunk_id,source_type,source_ref,source_hash,title,body,language,state,enrich_policy,materiality_score,created_at,updated_at) VALUES ('case-chunk','observation','test','hash','Aurora','Aurora','en','enrichable','manual',0.8,?,?)", (TS, TS))
    conn.execute("INSERT INTO candidate_claims (claim_id,chunk_id,subject,predicate,object_text,object_type,claim_scope,confidence,status,requires_human,created_at,updated_at) VALUES ('case-claim','case-chunk','Aurora service','configuration',?,'text','task',0.95,'candidate',0,?,?)", (value, TS, TS))


@pytest.mark.parametrize("same_object", [True, False])
def test_promotion_does_not_reuse_or_contradict_superseded_legacy_row(database, same_object):
    conn, _ = database
    insert(conn, fact("case-old", "OBSOLETE_SENTINEL"), fact("case-target", "OTHER_SENTINEL"))
    conn.execute("UPDATE canonical_facts SET superseded_by_fact_id='case-target' WHERE fact_id='case-old'")
    claim(conn, "OBSOLETE_SENTINEL" if same_object else "NEW_SENTINEL")
    result = claim_graph.promote_candidate(conn, "case-claim", mode="human_confirmed")
    assert result["promoted"] is True
    assert result["fact_id"] != "case-old"
    assert not result.get("reused_existing_fact", False)
    assert conn.execute("SELECT COUNT(*) FROM provenance_links WHERE subject_ref='case-old' AND source_kind='claim'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM knowledge_links WHERE relation_type='contradicts' AND (subject_ref='case-old' OR object_ref='case-old')").fetchone()[0] == 0


@pytest.mark.parametrize("failure_stage", ["first_link", "second_link", "ledger"])
def test_governance_failure_rolls_back_through_real_transaction(database, monkeypatch, failure_stage):
    _, path = database
    with db_utils.get_conn(str(path)) as transaction:
        insert(transaction, fact("case-old", "ORIGINAL_SENTINEL"), fact("case-new", "CURRENT_SENTINEL"))
    original = memory_audit.add_knowledge_link
    calls = 0

    def link(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == {"first_link": 1, "second_link": 2}.get(failure_stage):
            raise sqlite3.OperationalError("injected governance failure")
        return original(*args, **kwargs)

    def ledger(*args, **kwargs):
        raise sqlite3.OperationalError("injected governance failure")

    monkeypatch.setattr(memory_audit, "add_knowledge_link", link)
    if failure_stage == "ledger":
        monkeypatch.setattr(memory_audit, "record_memory_event", ledger)
    with pytest.raises(sqlite3.OperationalError, match="injected governance failure"):
        with db_utils.get_conn(str(path)) as transaction:
            memory_audit.govern_fact(transaction, "case-old", "supersede", target_fact_id="case-new", effective_at=END)
    restored = fresh_read(path)
    assert restored["links"] == 0
    assert all(f["valid_to"] is None and f["superseded_by_fact_id"] is None for f in restored["facts"])
    assert "ORIGINAL_SENTINEL" in restored["body"]
