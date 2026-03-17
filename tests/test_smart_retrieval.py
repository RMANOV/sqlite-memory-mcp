"""Tests for smart_retrieval.py — L1 multi-signal re-ranking."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, timedelta, timezone


from smart_retrieval import (
    RERANKING_POOL_SIZE,
    compute_composite_score,
    compute_recency_decay,
)


class TestRecencyDecay:
    def test_fresh_entity(self):
        now = datetime.now(timezone.utc).isoformat()
        decay = compute_recency_decay(now)
        assert 0.99 <= decay <= 1.0

    def test_old_entity(self):
        old = (datetime.now(timezone.utc) - timedelta(days=28)).isoformat()
        decay = compute_recency_decay(old, half_life_days=14)
        assert 0.24 <= decay <= 0.26  # 2^(-28/14) = 0.25

    def test_none_timestamp(self):
        assert compute_recency_decay(None) == 0.5

    def test_invalid_timestamp(self):
        assert compute_recency_decay("not-a-date") == 0.5

    def test_half_life_exact(self):
        half_ago = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        decay = compute_recency_decay(half_ago, half_life_days=14)
        assert 0.49 <= decay <= 0.51


class TestCompositeScore:
    def test_baseline(self):
        score = compute_composite_score(
            bm25_rank=-5.0,
            updated_at=datetime.now(timezone.utc).isoformat(),
            project=None,
            current_project=None,
            obs_count=0,
            relation_hops=0,
            has_canonical_facts=False,
            in_active_session=False,
        )
        assert score > 0

    def test_project_boost(self):
        now = datetime.now(timezone.utc).isoformat()
        base = compute_composite_score(-5.0, now, "proj-a", None, 5, 0, False, False)
        boosted = compute_composite_score(
            -5.0, now, "proj-a", "proj-a", 5, 0, False, False
        )
        assert boosted > base
        assert abs(boosted / base - 1.5) < 0.01

    def test_graph_proximity_1hop(self):
        now = datetime.now(timezone.utc).isoformat()
        base = compute_composite_score(-5.0, now, None, None, 5, 0, False, False)
        one_hop = compute_composite_score(-5.0, now, None, None, 5, 1, False, False)
        assert one_hop > base

    def test_graph_proximity_2hop(self):
        now = datetime.now(timezone.utc).isoformat()
        one_hop = compute_composite_score(-5.0, now, None, None, 5, 1, False, False)
        two_hop = compute_composite_score(-5.0, now, None, None, 5, 2, False, False)
        assert one_hop > two_hop  # 1-hop boost > 2-hop boost

    def test_fact_boost(self):
        now = datetime.now(timezone.utc).isoformat()
        no_facts = compute_composite_score(-5.0, now, None, None, 5, 0, False, False)
        with_facts = compute_composite_score(-5.0, now, None, None, 5, 0, True, False)
        assert with_facts > no_facts

    def test_session_boost(self):
        now = datetime.now(timezone.utc).isoformat()
        no_session = compute_composite_score(-5.0, now, None, None, 5, 0, False, False)
        in_session = compute_composite_score(-5.0, now, None, None, 5, 0, False, True)
        assert in_session > no_session

    def test_zero_obs(self):
        now = datetime.now(timezone.utc).isoformat()
        score = compute_composite_score(-5.0, now, None, None, 0, 0, False, False)
        assert score > 0  # Should not be zero

    def test_richness_increases_with_obs(self):
        now = datetime.now(timezone.utc).isoformat()
        few = compute_composite_score(-5.0, now, None, None, 1, 0, False, False)
        many = compute_composite_score(-5.0, now, None, None, 50, 0, False, False)
        assert many > few


class TestConstants:
    def test_reranking_pool_size(self):
        assert RERANKING_POOL_SIZE == 100
