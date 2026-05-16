"""Tests for per-tier decay inside ``Recall._rrf_fuse`` (Wave B — B2).

The per-tier ``score_weight`` callable lets the fuser apply a *different*
multiplier to each (doc, tier) contribution before reciprocal-rank summing.
This is what folds the per-representation half-life into RRF without losing
the rank-position signal.
"""

from __future__ import annotations

import pytest

from server import Recall, Store


# ──────────────────────────────────────────────
# Backward compatibility
# ──────────────────────────────────────────────


def test_default_behavior_unchanged_when_no_score_weight():
    tier_rankings = {"fts": [10, 20, 30]}
    weights = {"fts": 1.0}
    scores = Recall._rrf_fuse(tier_rankings, weights, k=60)

    # 1/(60+1) + 1/(60+2) + 1/(60+3) for doc at rank 0/1/2 respectively
    expected_first = 1.0 / 61.0
    assert scores[10] == pytest.approx(expected_first)
    # Order preserved
    assert scores[10] > scores[20] > scores[30]


def test_default_weights_kw_only():
    """score_weight is keyword-only so positional callers don't break."""
    import inspect
    sig = inspect.signature(Recall._rrf_fuse)
    assert sig.parameters["score_weight"].kind == inspect.Parameter.KEYWORD_ONLY


# ──────────────────────────────────────────────
# score_weight closure applies per (doc, tier)
# ──────────────────────────────────────────────


def test_score_weight_scales_contribution():
    tier_rankings = {"semantic": [1]}
    weights = {"semantic": 1.0}

    base = Recall._rrf_fuse(tier_rankings, weights, k=60)[1]
    halved = Recall._rrf_fuse(
        tier_rankings, weights, k=60,
        score_weight=lambda _doc, _tier: 0.5,
    )[1]
    assert halved == pytest.approx(base * 0.5)


def test_score_weight_can_be_different_per_tier():
    """A doc appearing in both ``multi_repr`` (decayed) and ``fts``
    (full strength) gets the *sum* of two differently weighted
    contributions, not a flat overall scalar."""
    tier_rankings = {
        "fts": [42],
        "multi_repr": [42],
    }
    weights = {"fts": 1.0, "multi_repr": 1.0}

    def weight_fn(_doc, tier):
        return 1.0 if tier == "fts" else 0.2  # multi_repr heavily aged

    scores = Recall._rrf_fuse(
        tier_rankings, weights, k=60, score_weight=weight_fn,
    )
    expected = 1.0 / 61.0 + 0.2 * (1.0 / 61.0)
    assert scores[42] == pytest.approx(expected)


def test_score_weight_exception_falls_back_to_one():
    """A buggy score_weight must not crash fusion — multiplier becomes 1.0."""
    def broken(_doc, _tier):
        raise RuntimeError("intentional")

    scores = Recall._rrf_fuse(
        {"fts": [7]}, {"fts": 1.0}, k=60, score_weight=broken,
    )
    assert scores[7] == pytest.approx(1.0 / 61.0)


# ──────────────────────────────────────────────
# Realistic combined scenario
# ──────────────────────────────────────────────


def test_fresh_summary_loses_to_fresh_raw_at_same_rank():
    """Two docs at rank 0 in different tiers: ``multi_repr`` hit on a 90-day-old
    summary (hl=30) vs ``fts`` hit on the same age content (hl=180). The fts
    contribution should dominate because raw content ages slower."""
    from datetime import datetime, timedelta, timezone

    ts = (datetime.now(timezone.utc) - timedelta(days=90)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    decay_summary = Store._decay_factor(ts, half_life_days=30)
    decay_raw_via_fts = Store._decay_factor(ts, half_life_days=180)

    def score_w(_doc, tier):
        return decay_summary if tier == "multi_repr" else decay_raw_via_fts

    rrf = Recall._rrf_fuse(
        {"multi_repr": [100], "fts": [200]},
        {"multi_repr": 1.0, "fts": 1.0},
        k=60, score_weight=score_w,
    )
    assert rrf[200] > rrf[100], (
        "fts (raw) match must outrank multi_repr (summary) match for "
        "documents of the same age"
    )
