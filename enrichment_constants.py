"""Shared constants for enrichment modules.

Extracted here to break the circular import between claim_graph and lazy_enrichment.
"""

_PREDICATE_BASE_CONFIDENCE: dict[str, float] = {
    "uses": 0.6,
    "depends_on": 0.7,
    "is": 0.4,
    "requires": 0.65,
    "produces": 0.6,
    "validates": 0.7,
    "contains": 0.5,
    "replaces": 0.55,
}
