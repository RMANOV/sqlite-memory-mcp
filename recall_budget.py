"""S1 — recall budget: pack a search result to a hard wire cap.

Why a separate module rather than ``db_utils``: the budget must never be
applied inside the shared observation readers (``serialize_entity``,
``fts_sync_entity``, ``bridge_change_summary``, ``export_entity_files``,
``export_entities_index``). ``bridge_sync_worker`` calls the first of those,
so budgeting there would silently ship truncated entities through bridge
sync. Keeping the primitive out of that file makes the mistake impossible
rather than merely discouraged.

Four decisions here were reached by measurement, and each replaced something
that looked correct:

- **Ask the tokenizer, don't imitate it.** Four successive Python
  normalisations were refuted against the real FTS path: ``.lower()``
  (ASCII-only in SQLite), NFKD (folds ligatures and full-width forms that
  unicode61 keeps), NFD+guard (breaks Cyrillic ``й`` — 7.6% of the corpus,
  turning ``който`` into ``които``), and Latin-only folding (misses ``ǿ``,
  ``ǽ``, and the ``Ꞗ`` case mapping). A temporary in-memory FTS5 table with
  the same tokenizer makes parity an identity instead of an approximation.

- **Width beats depth.** At a fixed budget, 40 entities x 2 observations
  recalls 80% of the oracle where 20 x 4 recalls 65% — at the same cost.
  Evidence-first is what makes this work: slot one already justifies the
  entity, so extra slots buy less than extra entities.

- **Window on the match, not the head.** The terms that caused a match sit
  past character 600 in the corpus's largest observations, so head
  truncation destroys exactly the evidence it was meant to preserve.

- **Coverage and jump are telemetry.** Their thresholds sat 0.3% from
  flipping on a hold-out set, and a strong jump means a *good* match for
  short proper-noun queries. Suppressing on them would hide real results,
  which is the more expensive error here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

# Frozen constants (spec MEMORY_RECALL_SPEC_20260814).
RECALL_WIRE_BUDGET = 16_000
RECALL_K = 40
RECALL_M = 2
RECALL_PREFIX = 120
RECALL_WINDOW = 600
_WIRE_FIELD_WIDTH = 7

# Tokenizers whose behaviour the probe is known to reproduce exactly, because
# it *is* them. Anything else fails closed rather than guessing.
_ALLOWED_TOKENIZERS = frozenset(
    {
        "unicode61 remove_diacritics 2",
        "unicode61 remove_diacritics 1",
        "unicode61",
        "ascii",
        "porter unicode61",
    }
)

# Options that bind an FTS table to an external content table. Copying these
# into a temporary probe would point it at rows we are not indexing.
_FORBIDDEN_OPTIONS = ("content", "content_rowid")

_TOKENIZE_RE = re.compile(r"tokenize\s*=\s*['\"]([^'\"]+)['\"]", re.I)


class ProbeSchemaError(RuntimeError):
    """The FTS schema cannot be reproduced safely. Fail closed."""


@dataclass(frozen=True)
class ProbeSpec:
    columns: list[str]
    tokenize: str
    extras: dict[str, str] = field(default_factory=dict)

    def create_sql(self, table: str = "probe") -> str:
        cols = ", ".join(self.columns)
        opts = "".join(f", {k}={v}" for k, v in sorted(self.extras.items()))
        return f'CREATE VIRTUAL TABLE {table} USING fts5({cols}{opts}, tokenize="{self.tokenize}")'


def fts_probe_spec(conn: sqlite3.Connection, table: str) -> ProbeSpec:
    """Read the live FTS definition and validate it can be mirrored.

    The tokenizer is read from ``sqlite_master`` rather than duplicated, so a
    schema change breaks the probe loudly instead of drifting from it.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None or not row[0]:
        raise ProbeSchemaError(f"no FTS table named {table!r}")
    sql = row[0]

    match = _TOKENIZE_RE.search(sql)
    tokenize = match.group(1).strip() if match else "unicode61"
    if tokenize not in _ALLOWED_TOKENIZERS:
        raise ProbeSchemaError(f"tokenizer not mirrorable: {tokenize!r}")

    body = sql[sql.index("(") + 1 : sql.rindex(")")]
    columns: list[str] = []
    extras: dict[str, str] = {}
    for part in _split_args(body):
        if "=" not in part:
            columns.append(part.strip())
            continue
        key = part.split("=", 1)[0].strip().lower()
        if key in _FORBIDDEN_OPTIONS:
            raise ProbeSchemaError(
                f"{table} is external-content ({key}=); a temporary probe cannot mirror it"
            )
        if key in ("detail", "prefix", "columnsize"):
            extras[key] = part.split("=", 1)[1].strip()
    if not columns:
        raise ProbeSchemaError(f"{table} declares no columns")
    return ProbeSpec(columns=columns, tokenize=tokenize, extras=extras)


def _split_args(body: str) -> list[str]:
    """Split a CREATE VIRTUAL TABLE argument list on top-level commas."""
    out, depth, current = [], 0, []
    quote = ""
    for ch in body:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        out.append("".join(current))
    return [p.strip() for p in out if p.strip()]


def evidence_ids(
    rows: list[tuple[int, str]], fts_query: str, spec: ProbeSpec
) -> set[int]:
    """Return the rowids the *real tokenizer* considers a match.

    ``rows`` is ``(observation_id, text)``. One batch insert, one MATCH —
    the probe answers with the index's own semantics, so no normalisation
    happens on the Python side at all.
    """
    if not rows or not fts_query.strip():
        return set()
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute(spec.create_sql("probe"))
        placeholders = ", ".join("?" for _ in spec.columns)
        blanks = [""] * (len(spec.columns) - 1)
        probe.executemany(
            f"INSERT INTO probe(rowid, {', '.join(spec.columns)}) "
            f"VALUES (?, {placeholders})",
            [(rid, *blanks, text) for rid, text in rows],
        )
        return {
            r[0]
            for r in probe.execute(
                "SELECT rowid FROM probe WHERE probe MATCH ?", (fts_query,)
            )
        }
    except sqlite3.Error:
        # A probe that cannot run must not silently claim "no evidence".
        raise ProbeSchemaError("evidence probe failed") from None
    finally:
        probe.close()


def window_around(text: str, positions: list[int], cap: int = RECALL_WINDOW) -> str:
    """Slice ``cap`` characters centred on the first match position.

    Head truncation would drop the match in exactly the observations where
    truncation bites hardest — the corpus's largest entry hides its terms
    past character 40000.
    """
    if len(text) <= cap:
        return text
    start = 0
    if positions:
        start = max(0, min(positions) - cap // 3)
    end = start + cap
    chunk = text[start:end]
    return ("…" if start > 0 else "") + chunk + ("…" if end < len(text) else "")


def _term_positions(text: str, terms: list[str]) -> list[int]:
    low = text.lower()
    found = [low.find(t.lower()) for t in terms]
    return [p for p in found if p >= 0]


def _select_observations(
    observations: list[dict], evidence: set, terms: list[str], m: int
) -> tuple[list[str], bool]:
    """Evidence-first: slot one proves the match, the rest add variety."""
    chosen: list[dict] = []
    carriers = [o for o in observations if o.get("id") in evidence]
    if carriers:
        chosen.append(max(carriers, key=lambda o: len(o.get("content", ""))))
    seen = {id(o) for o in chosen}
    for obs in observations:
        if len(chosen) >= m:
            break
        if id(obs) not in seen:
            chosen.append(obs)
    texts = [
        window_around(
            o.get("content", ""), _term_positions(o.get("content", ""), terms)
        )
        for o in chosen
    ]
    return texts, bool(carriers)


def pack_entities(
    entities: list[dict],
    *,
    evidence: set,
    query_terms: list[str],
    budget: int = RECALL_WIRE_BUDGET,
    m: int = RECALL_M,
    coverage: float | None = None,
    jump: float | None = None,
) -> tuple[list[dict], dict]:
    """Fill ``budget`` wire characters, widest-first, and account for it.

    ``K`` is an outcome, not a parameter: entities are added in rank order
    until the serialised payload would exceed the cap. The accounting block
    reports the full candidate pool so a fixed ``K`` cannot hide inside a
    budget that stopped varying.
    """
    packed: list[dict] = []
    degraded: list[dict] = []
    observations_returned = 0

    for entity in entities:
        texts, has_evidence = _select_observations(
            entity.get("observations", []), evidence, query_terms, m
        )
        record = {
            "name": entity.get("name"),
            "entityType": entity.get("entityType"),
            "observations": texts,
            "_evidence_status": "found" if has_evidence else "not_found",
        }
        if entity.get("project"):
            record["project"] = entity["project"]

        candidate = packed + [record]
        candidate_degraded = degraded + (
            []
            if has_evidence
            else [{"entity": record["name"], "reason": "evidence_not_found"}]
        )
        # Measure the payload as it will actually be sent, accounting block
        # included: the block grows with `degraded`, so measuring against a
        # stub silently under-counts and lets the result exceed the cap.
        trial = _accounting(
            entities,
            candidate,
            observations_returned + len(texts),
            budget,
            candidate_degraded,
            coverage,
            jump,
        )
        if _wire_size(candidate, trial) > budget and packed:
            break
        packed, degraded = candidate, candidate_degraded
        observations_returned += len(texts)

    accounting = _accounting(
        entities, packed, observations_returned, budget, degraded, coverage, jump
    )
    accounting["wire_bytes"] = _wire_value(_wire_size(packed, accounting))
    return packed, accounting


def _accounting(
    considered: list[dict],
    packed: list[dict],
    observations: int,
    budget: int,
    degraded: list[dict],
    coverage: float | None,
    jump: float | None,
) -> dict:
    status = "DEGRADED" if degraded else "OK"
    if status == "OK" and (coverage is not None or jump is not None):
        # Telemetry only. Their thresholds sat 0.3% from flipping on a
        # hold-out set, and hiding a real answer is the costlier failure —
        # so an uncalibrated signal downgrades the label, never the results.
        status = "UNCALIBRATED"
    block = {
        "entities_considered": len(considered),
        "entities_returned": len(packed),
        "observations_returned": observations,
        "truncated": len(packed) < len(considered),
        "budget_wire_bytes": budget,
        "wire_bytes": _wire_value(0),
        "status": status,
        "degraded": degraded,
    }
    if coverage is not None:
        block["coverage"] = coverage
    if jump is not None:
        block["jump"] = jump
    return block


def _wire_value(size: int) -> str:
    """Zero-padded so the field's own width never shifts what it measures.

    ``wire_bytes`` lives inside the payload it describes. An integer would
    change length as the value changes, so the measurement and the emitted
    value would disagree. A fixed-width string keeps one serialisation exact.
    """
    return str(size).zfill(_WIRE_FIELD_WIDTH)


def _wire_size(entities: list[dict], accounting: dict) -> int:
    return len(
        json.dumps(
            {"entities": entities, "_accounting": accounting}, ensure_ascii=False
        )
    )
