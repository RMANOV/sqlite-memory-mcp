# Canonical promotion and operator review

Automatic extraction records candidates, source references and heuristic confidence. Repeating an observation does not make it independent evidence, and confidence alone does not create a canonical fact. Inline lazy extraction, health sweeps and Layer 1 enrichment retain candidates for review.

## Review and approve one candidate

Use the local operator command with an existing database and a candidate ID:

```bash
sqlite-memory-approve --db /path/to/memory.db --kind candidate --claim-id CLAIM_ID
# Equivalent source/module invocation:
python -m promotion_governance --db /path/to/memory.db --kind lazy --claim-id LAZY_CLAIM_ID
```

The CLI displays the current claim, source, evidence, effective scope and policy. Untrusted content is delimited and terminal/bidi/control characters are visibly escaped. Review the content, then type the complete authoritative fingerprint printed outside that content. A blank or different response issues no approval. There is no approval-minting MCP tool or automatic confirmation flag.

The CLI rechecks the displayed snapshot after confirmation. If the source changed while being read, it refuses issuance and requires a new review. A successful command prints a receipt with `approval_id`, `claim_kind`, `claim_id`, `content_sha256` and `expires_at`. The receipt lasts 15 minutes and applies to one promotion of exactly that reviewed material.

Consume the already issued receipt through the existing MCP tool:

```python
promote_candidate(
    claim_id="CLAIM_ID",
    claim_kind="candidate",  # or "lazy" for the original lazy ID
    mode="human_confirmed",
    approval_id="APPROVAL_ID_FROM_OPERATOR_REVIEW",
)
```

The default `human_confirmed` argument is not itself proof of approval. Calls without a valid receipt retain the candidate and return a review-required result. The former `auto_layer1`, `multi_evidence` and `imported` promotion modes cannot create facts. `record_human_answer` records reported context; it does not mint approvals.

## Source freshness and atomicity

The receipt binds the claim and current source/evidence state, including effective scope and relevant promotion policy. Expired, used, mismatched or stale receipts are refused. Frozen, archived, stale or non-enrichable sources cannot be promoted. Sensitive scopes remain manually approvable through the same review workflow.

Ordinary chunks may be `enrichable`, `uncertain` or `awaiting_human` with a
supported enrichment policy. Self-contained `text`/`manual` sources are reviewed
as stored text. Observation, task/note and entity sources must resolve to their
current local records, whose contents and relevant state are included in the
review. Missing or unsupported source lineage is refused; a stored source hash
alone is not treated as current evidence.

Consumption, fact creation or reinforcement, candidate status, provenance, events and the resulting receipt record participate in one savepoint. A failed required write rolls them back together without committing unrelated work in a caller's outer transaction. Concurrent attempts cannot successfully consume the same receipt twice. Existing-fact reuse also requires matching scope and object type and a current, nonsuperseded lifecycle.

For a lazy candidate, the receipt binds the original observation and entity. Successful consumption creates a normal source chunk and candidate inside the same savepoint, then uses the common canonical writer. The resulting fact keeps the original lazy/observation lineage; failure leaves no partial normalization records.

## Trust boundaries and existing data

The operator CLI is a deliberate channel separate from MCP. It does not authenticate a biological human or prevent an actor with arbitrary shell/SQL access under the same operating-system account from modifying local data. A mode string, terminal presence or reported answer must not be described as such authentication.

Approval receipts are local state and are not part of bridge exports or imports. Explicitly trusted bridge replication may copy existing facts and their original validation labels; that is a source assertion, not newly verified local operator approval. Software-supplied ontology seeds are another separate origin. Historical facts and old `human_confirmed`, `auto_lazy` or `multi_evidence` labels are not retroactively revalidated or rewritten by this change.
