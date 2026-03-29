"""Experimental advanced context planning for task-scoped packs.

This module is intentionally optional. When disabled, context packing falls
back to the baseline greedy selector in ``context_packer.py``.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from typing import Any

_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-я_./:-]{4,}")


def _extract_keywords(text: str, *, limit: int = 24) -> list[str]:
    if not text:
        return []
    keywords: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(text.lower()):
        token = raw.strip("._:/-")
        if len(token) >= 4 and token not in seen:
            keywords.append(token)
            seen.add(token)
        for part in re.split(r"[_./:-]+", token):
            part = part.strip()
            if len(part) >= 4 and part not in seen:
                keywords.append(part)
                seen.add(part)
        if len(keywords) >= limit:
            break
    return keywords[:limit]


def _keyword_score(text: str, keywords: list[str], *, floor: float = 0.18) -> float:
    if not text or not keywords:
        return 0.0
    haystack = text.lower()
    matched = {kw for kw in keywords if kw in haystack}
    if not matched:
        return 0.0
    coverage = len(matched) / max(1, min(len(set(keywords)), 8))
    specificity = min(1.0, max(len(kw) for kw in matched) / 24.0)
    return min(1.0, floor + (coverage * 0.68) + (specificity * 0.14))


def _name_overlap_score(text: str, name_scores: dict[str, float]) -> float:
    if not text or not name_scores:
        return 0.0
    haystack = text.lower()
    best = 0.0
    for name, score in name_scores.items():
        if name and name in haystack:
            best = max(best, score)
    return best


def _dedupe_keywords(*groups: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for keyword in group:
            if keyword not in seen:
                seen.add(keyword)
                result.append(keyword)
            if len(result) >= limit:
                return result
    return result


def _load_seed_names(
    conn: sqlite3.Connection,
    seed_entity_ids: dict[int, float],
    relevant_names: dict[str, float],
) -> dict[str, float]:
    names = {name.lower(): score for name, score in relevant_names.items() if name}
    if seed_entity_ids:
        ph = ",".join("?" * len(seed_entity_ids))
        rows = conn.execute(
            f"SELECT id, name FROM entities WHERE id IN ({ph})",
            list(seed_entity_ids),
        ).fetchall()
        for row in rows:
            if row["name"]:
                lowered = row["name"].lower()
                names[lowered] = max(
                    names.get(lowered, 0.0),
                    seed_entity_ids.get(int(row["id"]), 0.0),
                )
    return names


def _load_relation_neighbors(
    conn: sqlite3.Connection,
    seed_entity_ids: dict[int, float],
    *,
    limit: int,
) -> tuple[dict[int, float], dict[str, float], list[str]]:
    if not seed_entity_ids:
        return {}, {}, []

    seed_ids = list(seed_entity_ids)
    ph = ",".join("?" * len(seed_ids))
    rows = conn.execute(
        f"""
        SELECT rel.seed_id, rel.neighbor_id, rel.relation_type, e.name
        FROM (
            SELECT from_id AS seed_id, to_id AS neighbor_id, relation_type
            FROM relations WHERE from_id IN ({ph})
            UNION ALL
            SELECT to_id AS seed_id, from_id AS neighbor_id, relation_type
            FROM relations WHERE to_id IN ({ph})
        ) rel
        JOIN entities e ON e.id = rel.neighbor_id
        LIMIT ?
        """,
        seed_ids + seed_ids + [limit],
    ).fetchall()

    entity_scores: dict[int, float] = {}
    name_scores: dict[str, float] = {}
    keywords: list[str] = []
    for row in rows:
        base = seed_entity_ids.get(int(row["seed_id"]), 0.45)
        boost = max(0.22, min(0.82, base * 0.72))
        neighbor_id = int(row["neighbor_id"])
        entity_scores[neighbor_id] = max(entity_scores.get(neighbor_id, 0.0), boost)
        if row["name"]:
            lowered = row["name"].lower()
            name_scores[lowered] = max(name_scores.get(lowered, 0.0), boost)
            keywords.extend(_extract_keywords(row["name"], limit=4))
        if row["relation_type"]:
            keywords.extend(_extract_keywords(row["relation_type"], limit=3))

    return entity_scores, name_scores, keywords


def _load_claim_fact_keywords(
    conn: sqlite3.Connection,
    seed_names: dict[str, float],
    *,
    limit: int,
) -> list[str]:
    if not seed_names:
        return []

    names = list(seed_names)[:12]
    ph = ",".join("?" * len(names))
    keywords: list[str] = []

    fact_rows = conn.execute(
        f"""
        SELECT predicate, object_text
        FROM canonical_facts
        WHERE lower(subject) IN ({ph})
        ORDER BY confidence DESC, updated_at DESC
        LIMIT ?
        """,
        names + [limit],
    ).fetchall()
    for row in fact_rows:
        keywords.extend(_extract_keywords(row["predicate"] or "", limit=4))
        keywords.extend(_extract_keywords(row["object_text"] or "", limit=6))

    claim_rows = conn.execute(
        f"""
        SELECT predicate, object_text
        FROM candidate_claims
        WHERE status = 'candidate' AND lower(subject) IN ({ph})
        ORDER BY confidence DESC, updated_at DESC
        LIMIT ?
        """,
        names + [limit],
    ).fetchall()
    for row in claim_rows:
        keywords.extend(_extract_keywords(row["predicate"] or "", limit=4))
        keywords.extend(_extract_keywords(row["object_text"] or "", limit=6))

    return keywords


def build_strategy(
    conn: sqlite3.Connection,
    *,
    target_ref: str | None,
    task_query: str,
    linked_ids: list[int],
    task_keywords: list[str],
    relevant_names: dict[str, float],
    relevant_entity_ids: dict[int, float],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build a bounded advanced strategy for task-scoped context retrieval."""
    enabled = bool(config.get("advanced_context_enabled")) and bool(target_ref)
    strategy: dict[str, Any] = {
        "enabled": enabled,
        "query_expansion_used": False,
        "submodular_enabled": bool(
            config.get("advanced_context_submodular_enabled", True)
        ),
        "seed_entity_scores": {},
        "seed_name_scores": {},
        "expanded_entity_scores": {},
        "expanded_name_scores": {},
        "expansion_keywords": [],
        "target_keywords": task_keywords[:],
        "metadata": {
            "seed_entities": 0,
            "expanded_entities": 0,
            "expansion_keywords": 0,
        },
    }
    if not enabled:
        return strategy

    max_seeds = max(2, int(config.get("advanced_context_max_seed_entities", 8)))
    max_related = max(4, int(config.get("advanced_context_max_related_entities", 12)))
    max_keywords = max(
        8, int(config.get("advanced_context_max_expansion_keywords", 24))
    )

    seed_entity_scores = dict(relevant_entity_ids)
    for eid in linked_ids:
        seed_entity_scores[eid] = max(seed_entity_scores.get(eid, 0.0), 0.78)
    ranked_seed_ids = sorted(
        seed_entity_scores.items(),
        key=lambda item: (-item[1], item[0]),
    )[:max_seeds]
    seed_entity_scores = {eid: score for eid, score in ranked_seed_ids}
    seed_name_scores = _load_seed_names(conn, seed_entity_scores, relevant_names)

    strategy["seed_entity_scores"] = seed_entity_scores
    strategy["seed_name_scores"] = seed_name_scores

    if config.get("query_expansion_enabled"):
        strategy["query_expansion_used"] = True
        rel_entity_scores, rel_name_scores, rel_keywords = _load_relation_neighbors(
            conn,
            seed_entity_scores,
            limit=max_related,
        )
        claim_fact_keywords = _load_claim_fact_keywords(
            conn,
            seed_name_scores,
            limit=max_related,
        )

        expanded_entity_scores = dict(rel_entity_scores)
        expanded_name_scores = dict(rel_name_scores)
        for eid, score in seed_entity_scores.items():
            expanded_entity_scores[eid] = max(
                expanded_entity_scores.get(eid, 0.0), score
            )
        for name, score in seed_name_scores.items():
            expanded_name_scores[name] = max(expanded_name_scores.get(name, 0.0), score)

        expansion_keywords = _dedupe_keywords(
            task_keywords,
            rel_keywords,
            claim_fact_keywords,
            _extract_keywords(task_query, limit=12),
            limit=max_keywords,
        )
        strategy["expanded_entity_scores"] = expanded_entity_scores
        strategy["expanded_name_scores"] = expanded_name_scores
        strategy["expansion_keywords"] = expansion_keywords
    else:
        strategy["expanded_entity_scores"] = dict(seed_entity_scores)
        strategy["expanded_name_scores"] = dict(seed_name_scores)
        strategy["expansion_keywords"] = task_keywords[:max_keywords]

    strategy["metadata"] = {
        "seed_entities": len(strategy["seed_entity_scores"]),
        "expanded_entities": len(strategy["expanded_entity_scores"]),
        "expansion_keywords": len(strategy["expansion_keywords"]),
    }
    return strategy


def compute_strategy_match(
    strategy: dict[str, Any],
    *texts: str | None,
    entity_id: int | str | None = None,
    entity_name: str | None = None,
) -> float:
    """Return additive relevance boost from the advanced strategy."""
    if not strategy.get("enabled"):
        return 0.0

    best = 0.0
    if entity_id is not None:
        try:
            best = max(
                best,
                float(
                    strategy.get("expanded_entity_scores", {}).get(int(entity_id), 0.0)
                ),
            )
        except (TypeError, ValueError):
            best = max(
                best,
                float(
                    strategy.get("expanded_entity_scores", {}).get(str(entity_id), 0.0)
                ),
            )

    if entity_name:
        lowered = entity_name.lower()
        best = max(
            best,
            float(strategy.get("expanded_name_scores", {}).get(lowered, 0.0)),
        )

    joined = " ".join(text for text in texts if text)
    if joined:
        best = max(
            best,
            _keyword_score(joined, strategy.get("expansion_keywords", [])),
        )
        best = max(
            best,
            _name_overlap_score(joined, strategy.get("expanded_name_scores", {})),
        )
    return min(1.0, best)


def _normalized_item_tokens(item: dict[str, Any]) -> set[str]:
    cached = item.get("_selector_tokens")
    if isinstance(cached, set):
        return cached
    tokens = set(_extract_keywords(item.get("text", ""), limit=18))
    item["_selector_tokens"] = tokens
    return tokens


def _coverage_keys(item: dict[str, Any]) -> set[str]:
    return {str(key) for key in item.get("coverage_keys", []) if key}


def select_context_items(
    items: list[dict[str, Any]],
    *,
    budget: int,
    max_items_by_type: dict[str, int],
    max_chunks_per_group: int,
    strategy: dict[str, Any],
) -> dict[str, Any]:
    """Select context items with marginal-gain ranking and deterministic tie-breaks."""
    remaining = sorted(
        items,
        key=lambda item: (
            -float(item.get("score", 0.0)),
            -float(item.get("relevance", 0.0)),
            -float(item.get("trust", 0.0)),
            str(item.get("type", "")),
            str(item.get("id", "")),
        ),
    )

    selected: list[dict[str, Any]] = []
    tokens_used = 0
    type_counts: dict[str, int] = defaultdict(int)
    chunk_group_counts: dict[str, int] = defaultdict(int)
    seen_texts: set[str] = set()
    covered_keys: set[str] = set()
    selected_tokens: set[str] = set()
    selected_types: set[str] = set()
    planner_keywords = set(strategy.get("expansion_keywords", []))

    while remaining:
        best_item: dict[str, Any] | None = None
        best_rank: tuple[Any, ...] | None = None

        for item in remaining:
            item_type = str(item.get("type", "chunk"))
            item_tokens = int(item.get("tokens", 0))
            if tokens_used + item_tokens > budget:
                continue
            if type_counts[item_type] >= max_items_by_type[item_type]:
                continue
            if item_type == "chunk":
                group_key = item.get("group_key")
                if group_key and chunk_group_counts[group_key] >= max_chunks_per_group:
                    continue

            dedupe_key = " ".join(str(item.get("text", "")).lower().split())
            if dedupe_key in seen_texts:
                continue

            item_word_set = _normalized_item_tokens(item)
            overlap = len(item_word_set & selected_tokens)
            novelty = (
                1.0
                if not item_word_set
                else 1.0 - (overlap / max(1, len(item_word_set)))
            )
            new_coverage = len(_coverage_keys(item) - covered_keys)
            type_bonus = 0.08 if item_type not in selected_types else 0.0
            expansion_hits = len(item_word_set & planner_keywords)
            group_penalty = (
                0.18
                if item_type == "chunk" and item.get("group_key") in chunk_group_counts
                else 0.0
            )
            marginal_gain = (
                float(item.get("score", 0.0)) * (0.58 + (novelty * 0.42))
                + min(new_coverage, 4) * 0.09
                + type_bonus
                + min(expansion_hits, 3) * 0.03
                - group_penalty
            )

            rank = (
                round(marginal_gain, 6),
                float(item.get("score", 0.0)),
                float(item.get("relevance", 0.0)),
                -item_tokens,
                str(item.get("type", "")),
                str(item.get("id", "")),
            )
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best_item = item

        if best_item is None:
            break

        remaining.remove(best_item)
        selected.append(best_item)
        tokens_used += int(best_item.get("tokens", 0))
        item_type = str(best_item.get("type", "chunk"))
        type_counts[item_type] += 1
        selected_types.add(item_type)
        if item_type == "chunk" and best_item.get("group_key"):
            chunk_group_counts[best_item["group_key"]] += 1
        covered_keys.update(_coverage_keys(best_item))
        selected_tokens.update(_normalized_item_tokens(best_item))
        seen_texts.add(" ".join(str(best_item.get("text", "")).lower().split()))

    return {
        "selected": selected,
        "tokens_used": tokens_used,
        "metadata": {
            "selection_strategy": "submodular",
            "covered_keys": len(covered_keys),
            "selected_types": len(selected_types),
        },
    }
