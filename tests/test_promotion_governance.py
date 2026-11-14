"""Promotion requires a separately reviewed snapshot, never repetition or a label."""
import json
import sqlite3
import io
from concurrent.futures import ThreadPoolExecutor

import pytest

import claim_graph
import db_utils
import lazy_enrichment
import server
from schema import init_db


TS = "2026-11-14T12:00:00+00:00"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "promotion.db"
    init_db(str(path))
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn, path
    conn.close()


def candidate(conn, claim_id="review-claim", *, scope="memory", state="enrichable", body="ReviewProbe uses Rust."):
    chunk = "source-" + claim_id
    conn.execute(
        "INSERT INTO context_chunks(chunk_id,source_type,source_ref,source_hash,title,body,state,enrich_policy,created_at,updated_at) "
        "VALUES (?, 'text', ?, 'stored-hash-is-not-authority', 'Review source', ?, ?, 'manual', ?, ?)",
        (chunk, chunk, body, state, TS, TS),
    )
    conn.execute(
        "INSERT INTO candidate_claims(claim_id,chunk_id,subject,predicate,object_text,object_type,claim_scope,confidence,status,requires_human,created_at,updated_at) "
        "VALUES (?, ?, 'ReviewProbe', 'uses', 'Rust', 'text', ?, 0.95, 'candidate', 1, ?, ?)",
        (claim_id, chunk, scope, TS, TS),
    )
    conn.execute(
        "INSERT INTO claim_evidence(evidence_id,claim_id,evidence_type,evidence_ref,weight,excerpt,created_at) "
        "VALUES (?, ?, 'chunk', ?, 1, ?, ?)",
        ("evidence-"+claim_id, claim_id, chunk, body, TS),
    )
    return claim_id


def test_replayed_server_observation_is_not_independent_evidence(database, monkeypatch):
    conn, path = database
    monkeypatch.setattr(server, "_get_conn", lambda: db_utils.get_conn(str(path)))
    monkeypatch.setattr(server, "_VEC_AVAILABLE", False)
    body = "ReplayProbe uses ImaginaryEngine."
    server.create_entities([{"name":"ReplayProbe", "entityType":"project", "observations":[body]}])
    for _ in range(5):
        assert json.loads(server.add_observations([{"entityName":"ReplayProbe", "contents":[body]}]))["added"] == 0
    assert conn.execute("SELECT count(*) FROM canonical_facts WHERE subject='ReplayProbe'").fetchone()[0] == 0
    row = conn.execute("SELECT status,hit_count FROM lazy_claims WHERE subject='ReplayProbe'").fetchone()
    assert row["status"] == "candidate"
    assert row["hit_count"] == 1


def test_human_confirmed_argument_is_not_an_approval(database):
    conn, _ = database
    claim_id = candidate(conn)
    result = claim_graph.promote_candidate(conn, claim_id, mode="human_confirmed")
    assert result.get("promoted") is False
    assert conn.execute("SELECT count(*) FROM canonical_facts WHERE subject='ReviewProbe'").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM candidate_claims WHERE claim_id=?", (claim_id,)).fetchone()[0] == "candidate"


def test_rejected_lazy_claim_cannot_be_promoted_by_confidence(database):
    conn, _ = database
    eid = conn.execute("INSERT INTO entities(name,entity_type,created_at,updated_at) VALUES ('RejectedProbe','project',?,?)", (TS,TS)).lastrowid
    text = "RejectedProbe uses ImaginaryEngine."
    oid = conn.execute("INSERT INTO observations(entity_id,content,created_at) VALUES (?,?,?)", (eid,text,TS)).lastrowid
    lazy_enrichment.extract_inline_claims(conn,eid,oid,text)
    row = conn.execute("SELECT claim_id FROM lazy_claims WHERE entity_id=?",(eid,)).fetchone()
    conn.execute("UPDATE lazy_claims SET status='rejected' WHERE claim_id=?",(row[0],))
    assert lazy_enrichment.auto_promote_claim(conn,row[0],0.99) is None
    assert conn.execute("SELECT status FROM lazy_claims WHERE claim_id=?",(row[0],)).fetchone()[0] == "rejected"
    assert conn.execute("SELECT count(*) FROM canonical_facts WHERE subject='RejectedProbe'").fetchone()[0] == 0


def approve(conn, claim_id, kind="candidate"):
    import promotion_governance as governance
    review = governance.prepare_review(conn, kind, claim_id)
    return governance.issue_approval(conn, kind, claim_id, review["content_sha256"])


def lazy_candidate(conn):
    eid = conn.execute("INSERT INTO entities(name,entity_type,project,created_at,updated_at) VALUES ('LazyReview','project','private-fixture',?,?)", (TS,TS)).lastrowid
    body = "LazyReview uses Rust."
    oid = conn.execute("INSERT INTO observations(entity_id,content,created_at) VALUES (?,?,?)", (eid,body,TS)).lastrowid
    lazy_enrichment.extract_inline_claims(conn,eid,oid,body)
    return conn.execute("SELECT claim_id FROM lazy_claims WHERE entity_id=?", (eid,)).fetchone()[0]


@pytest.mark.parametrize("kind", ["candidate", "lazy"])
def test_separately_approved_snapshot_promotes_once_with_lineage(database, kind):
    conn, _ = database
    cid = candidate(conn) if kind == "candidate" else lazy_candidate(conn)
    receipt = approve(conn,cid,kind)
    result = claim_graph.promote_candidate(conn,cid,approval_id=receipt["approval_id"],claim_kind=kind)
    assert result["promoted"] is True
    fid = result["fact_id"]
    fact = conn.execute("SELECT * FROM canonical_facts WHERE fact_id=?", (fid,)).fetchone()
    assert fact["validation_mode"] == "human_confirmed"
    assert fact["source_claim_id"] is not None
    assert conn.execute("SELECT status FROM candidate_claims WHERE claim_id=?", (fact["source_claim_id"],)).fetchone()[0] == "promoted"
    row = conn.execute("SELECT * FROM promotion_approvals WHERE approval_id=?", (receipt["approval_id"],)).fetchone()
    assert row["consumed_at"] and row["fact_id"] == fid
    assert conn.execute("SELECT count(*) FROM provenance_links WHERE subject_kind='fact' AND subject_ref=?", (fid,)).fetchone()[0] >= 2
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    again = claim_graph.promote_candidate(conn,cid,approval_id=receipt["approval_id"],claim_kind=kind)
    assert again["promoted"] is False
    if kind == "lazy":
        assert conn.execute("SELECT promoted_to_fact_id FROM lazy_claims WHERE claim_id=?",(cid,)).fetchone()[0] == fid
        assert conn.execute("SELECT count(*) FROM provenance_links WHERE subject_ref=? AND source_kind='observation'", (fid,)).fetchone()[0] == 1


@pytest.mark.parametrize("mutation", [
    "UPDATE context_chunks SET body='changed body'",
    "UPDATE context_chunks SET state='frozen'",
    "UPDATE candidate_claims SET object_text='Go' WHERE claim_id='review-claim'",
    "UPDATE candidate_claims SET claim_scope='export' WHERE claim_id='review-claim'",
    "UPDATE claim_evidence SET excerpt='changed excerpt'",
    "INSERT INTO context_annotations(annotation_id,chunk_id,author_type,annotation_type,body,created_at) VALUES ('changed','source-review-claim','human','note','changed annotation','2026-11-14')",
])
def test_changed_reviewed_material_refuses_without_consuming(database, mutation):
    conn, _ = database
    cid = candidate(conn)
    receipt = approve(conn,cid)
    conn.execute(mutation)
    result = claim_graph.promote_candidate(conn,cid,approval_id=receipt["approval_id"])
    assert result["promoted"] is False
    assert conn.execute("SELECT consumed_at FROM promotion_approvals").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM canonical_facts WHERE subject='ReviewProbe'").fetchone()[0] == 0


def test_issuance_checks_the_snapshot_shown_before_confirmation(database):
    import promotion_governance as governance
    conn, _ = database
    cid = candidate(conn)
    review = governance.prepare_review(conn,"candidate",cid)
    conn.execute("UPDATE context_chunks SET body='changed while reading'")
    with pytest.raises(governance.ApprovalError, match="changed"):
        governance.issue_approval(conn,"candidate",cid,review["content_sha256"])
    assert conn.execute("SELECT count(*) FROM promotion_approvals").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["multi_evidence", "imported", "auto_layer1", "forged"])
def test_nonhuman_modes_do_not_consume_even_valid_receipt(database, mode):
    conn, _ = database
    cid = candidate(conn)
    receipt = approve(conn,cid)
    result = claim_graph.promote_candidate(conn,cid,mode=mode,approval_id=receipt["approval_id"])
    assert result["promoted"] is False
    assert conn.execute("SELECT consumed_at FROM promotion_approvals").fetchone()[0] is None


@pytest.mark.parametrize("state", ["no_enrich", "frozen", "archived", "stale", "unknown", "enriched"])
def test_ineligible_source_cannot_be_approved(database, state):
    import promotion_governance as governance
    conn, _ = database
    cid = candidate(conn,state=state)
    with pytest.raises(governance.ApprovalError):
        approve(conn,cid)


@pytest.mark.parametrize("field,value", [
    ("expires_at", "2000-01-01T00:00:00+00:00"),
    ("approved_at", "2999-01-01T00:00:00+00:00"),
    ("expires_at", "not-a-date"),
    ("content_sha256", "forged"),
    ("approved_via", "mcp"),
])
def test_invalid_receipt_is_not_authority(database, field, value):
    conn, _ = database
    cid = candidate(conn)
    receipt = approve(conn,cid)
    conn.execute(f"UPDATE promotion_approvals SET {field}=?", (value,))
    result = claim_graph.promote_candidate(conn,cid,approval_id=receipt["approval_id"])
    assert result["promoted"] is False
    assert conn.execute("SELECT consumed_at FROM promotion_approvals").fetchone()[0] is None


@pytest.mark.parametrize("kind", ["candidate", "lazy"])
@pytest.mark.parametrize("failure", ["provenance", "event", "impact"])
def test_required_write_failure_rolls_back_receipt_and_knowledge(database, monkeypatch, kind, failure):
    conn, _ = database
    cid = candidate(conn) if kind == "candidate" else lazy_candidate(conn)
    receipt = approve(conn,cid,kind)
    before = {table:conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("context_chunks","candidate_claims","canonical_facts","provenance_links","memory_events")}
    def fail(*args,**kwargs):
        raise sqlite3.OperationalError("injected audit failure")
    if failure == "provenance":
        monkeypatch.setattr(claim_graph,"add_provenance_link",fail)
    elif failure == "event":
        monkeypatch.setattr(claim_graph,"record_memory_event",fail)
    else:
        import impact_graph
        monkeypatch.setattr(impact_graph,"add_impact_edge",fail)
    conn.execute("BEGIN")
    conn.execute("INSERT INTO entities(name,entity_type,created_at,updated_at) VALUES ('unrelated pending','test',?,?)", (TS,TS))
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        claim_graph.promote_candidate(conn,cid,approval_id=receipt["approval_id"],claim_kind=kind)
    assert conn.in_transaction
    assert conn.execute("SELECT count(*) FROM entities WHERE name='unrelated pending'").fetchone()[0] == 1
    assert conn.execute("SELECT consumed_at FROM promotion_approvals").fetchone()[0] is None
    assert {table:conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in before} == before
    conn.rollback()


def test_only_one_concurrent_consumer_succeeds(database):
    conn, path = database
    cid = candidate(conn)
    receipt = approve(conn,cid)
    def consume():
        with db_utils.get_conn(str(path)) as other:
            return claim_graph.promote_candidate(other,cid,approval_id=receipt["approval_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sum(r["promoted"] for r in results) == 1
    assert conn.execute("SELECT count(*) FROM canonical_facts WHERE subject='ReviewProbe'").fetchone()[0] == 1


def test_safe_cli_preserves_text_but_escapes_terminal_controls(database):
    import promotion_governance as governance
    conn, path = database
    cid = candidate(conn,body="Текст\x1b[2J\rFORGED\u202e\u200b")
    review = governance.prepare_review(conn,"candidate",cid)
    output=io.StringIO()
    code=governance.main(["--db",str(path),"--kind","candidate","--claim-id",cid],input_stream=io.StringIO(review["content_sha256"]+"\n"),output_stream=output)
    assert code == 0
    rendered=output.getvalue()
    assert "Текст" in rendered
    assert "\\u001b" in rendered and "\\u000d" in rendered and "\\u202e" in rendered and "\\u200b" in rendered
    assert not any(c in rendered for c in ("\x1b","\r","\u202e","\u200b"))
    assert rendered.rfind("Fingerprint:") > rendered.find("END UNTRUSTED SOURCE")
    assert conn.execute("SELECT count(*) FROM promotion_approvals").fetchone()[0] == 1


def test_real_extractor_evidence_can_be_reviewed_and_promoted(database):
    conn, _ = database
    candidate(conn)
    conn.execute("DELETE FROM claim_evidence")
    conn.execute("DELETE FROM candidate_claims")
    extracted=claim_graph.extract_candidate_claims(conn,"source-review-claim")
    assert extracted["claims_extracted"] == 1
    cid=extracted["claims"][0]["claim_id"]
    assert conn.execute("SELECT evidence_type FROM claim_evidence WHERE claim_id=?",(cid,)).fetchone()[0] == "source_chunk"
    receipt=approve(conn,cid)
    assert claim_graph.promote_candidate(conn,cid,approval_id=receipt["approval_id"])["promoted"] is True


@pytest.mark.parametrize("source_kind", ["observation", "task"])
def test_underlying_live_source_change_invalidates_copied_chunk(database, source_kind):
    conn, _ = database
    cid=candidate(conn)
    if source_kind == "observation":
        eid=conn.execute("INSERT INTO entities(name,entity_type,created_at,updated_at) VALUES ('Origin','project',?,?)",(TS,TS)).lastrowid
        ref=conn.execute("INSERT INTO observations(entity_id,content,created_at) VALUES (?, 'ReviewProbe uses Rust.', ?)",(eid,TS)).lastrowid
    else:
        ref="task-source"
        conn.execute("INSERT INTO tasks(id,title,description,created_at,updated_at) VALUES (?, 'Review source', 'ReviewProbe uses Rust.', ?, ?)",(ref,TS,TS))
    conn.execute("UPDATE context_chunks SET source_type=?,source_ref=?",(source_kind,str(ref)))
    receipt=approve(conn,cid)
    if source_kind == "observation":
        conn.execute("UPDATE observations SET content='ReviewProbe uses Python.' WHERE id=?",(ref,))
    else:
        conn.execute("UPDATE tasks SET description='ReviewProbe uses Python.' WHERE id=?",(ref,))
    result=claim_graph.promote_candidate(conn,cid,approval_id=receipt["approval_id"])
    assert result["promoted"] is False
    assert conn.execute("SELECT consumed_at FROM promotion_approvals").fetchone()[0] is None


def test_extraction_does_not_downgrade_sensitive_source_with_scope_hint(database):
    conn, _=database
    candidate(conn,body="BridgeProbe uses bridge sync.")
    result=claim_graph.extract_candidate_claims(conn,"source-review-claim",scope_hint="memory")
    assert result["claims"][0]["scope"] == "bridge"
    assert result["claims"][0]["requires_human"] is True


def test_cli_aborts_if_source_changes_while_operator_reads(database):
    import promotion_governance as governance
    conn,path=database
    cid=candidate(conn)
    review=governance.prepare_review(conn,"candidate",cid)
    class ChangedInput:
        def readline(self):
            conn.execute("UPDATE context_chunks SET body='changed while operator reads'")
            return review["content_sha256"]+"\n"
    output=io.StringIO()
    assert governance.main(["--db",str(path),"--kind","candidate","--claim-id",cid],input_stream=ChangedInput(),output_stream=output) == 1
    assert "changed" in output.getvalue()
    assert conn.execute("SELECT count(*) FROM promotion_approvals").fetchone()[0] == 0


def test_all_automatic_entrypoints_leave_candidates(database):
    conn,_=database
    cid=candidate(conn)
    lazy_id=lazy_candidate(conn)
    conn.execute("UPDATE lazy_claims SET confidence=0.99 WHERE claim_id=?",(lazy_id,))
    assert claim_graph.auto_promote_layer1(conn,[{"claim_id":cid,"subject":"ReviewProbe","predicate":"uses","object":"Rust","confidence":0.99,"requires_human":False}]) == []
    report=lazy_enrichment.run_health_sweep(conn)
    assert report["promoted"] == []
    assert report["summary"]["claims_awaiting_review"] == 1
    assert conn.execute("SELECT count(*) FROM canonical_facts WHERE subject IN ('ReviewProbe','LazyReview')").fetchone()[0] == 0


def test_cross_entity_copy_and_mismatched_observation_do_not_inflate_claim(database):
    conn,_=database
    first=lazy_candidate(conn)
    eid=conn.execute("INSERT INTO entities(name,entity_type,created_at,updated_at) VALUES ('AnotherEntity','test',?,?)",(TS,TS)).lastrowid
    body="LazyReview uses Rust."
    oid=conn.execute("INSERT INTO observations(entity_id,content,created_at) VALUES (?,?,?)",(eid,body,TS)).lastrowid
    assert lazy_enrichment.extract_inline_claims(conn,eid,oid,"LazyReview uses FORGED.") == 0
    assert lazy_enrichment.extract_inline_claims(conn,eid,oid,body) == 1
    assert conn.execute("SELECT count(*) FROM lazy_claims WHERE subject='LazyReview'").fetchone()[0] == 2
    assert conn.execute("SELECT hit_count FROM lazy_claims WHERE claim_id=?",(first,)).fetchone()[0] == 1
