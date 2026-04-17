"""Tests for src/fusion.py — v7.0 Phase H."""

import pytest

from fusion import EvidenceSource, combine, from_ranked_results, UNKNOWN


# ──────────────────────────────────────────────
# Basic combination
# ──────────────────────────────────────────────

def test_single_source_returns_normalised():
    s = EvidenceSource("a", {"H1": 0.8, "H2": 0.2})
    out = combine([s])
    assert out["best_hypothesis"] == "H1"
    assert abs(out["best_mass"] - 0.8) < 1e-6


def test_two_sources_agree_boost_common():
    s1 = EvidenceSource("a", {"H1": 0.7, UNKNOWN: 0.3})
    s2 = EvidenceSource("b", {"H1": 0.6, UNKNOWN: 0.4})
    out = combine([s1, s2])
    assert out["best_hypothesis"] == "H1"
    # Both agreeing on H1 should raise its mass above both priors
    assert out["best_mass"] > 0.7


def test_two_sources_conflict_reduces_confidence():
    s1 = EvidenceSource("a", {"H1": 0.9, UNKNOWN: 0.1})
    s2 = EvidenceSource("b", {"H2": 0.9, UNKNOWN: 0.1})
    out = combine([s1, s2])
    assert out["conflict"] > 0.5
    # Best hypothesis should still tie-break but mass is low
    assert out["best_mass"] < 0.7


def test_total_conflict_falls_back_to_unknown():
    s1 = EvidenceSource("a", {"H1": 1.0})
    s2 = EvidenceSource("b", {"H2": 1.0})
    out = combine([s1, s2])
    assert out["best_hypothesis"] == UNKNOWN


def test_empty_sources_returns_unknown():
    out = combine([])
    assert out["best_hypothesis"] == UNKNOWN
    assert out["best_mass"] == 1.0


def test_unknown_source_acts_as_neutral():
    s1 = EvidenceSource("a", {"H1": 0.8, UNKNOWN: 0.2})
    s2 = EvidenceSource("b", {UNKNOWN: 1.0})  # fully uninformative
    out = combine([s1, s2])
    # H1 should be preserved since s2 is uninformative
    assert out["best_hypothesis"] == "H1"
    assert abs(out["best_mass"] - 0.8) < 1e-6


def test_three_sources_agree():
    srcs = [EvidenceSource(f"s{i}", {"H": 0.6, UNKNOWN: 0.4}) for i in range(3)]
    out = combine(srcs)
    assert out["best_hypothesis"] == "H"
    # Confidence should grow with every agreeing piece of evidence
    assert out["best_mass"] > 0.85


def test_zero_mass_source_handled():
    s = EvidenceSource("zero", {"H": 0.0})
    out = combine([s])
    assert out["best_hypothesis"] == UNKNOWN


# ──────────────────────────────────────────────
# from_ranked_results
# ──────────────────────────────────────────────

def test_from_ranked_results_distributes_by_score():
    results = [
        {"id": "A", "score": 3.0},
        {"id": "B", "score": 1.0},
    ]
    src = from_ranked_results("src1", results)
    assert src.masses[UNKNOWN] == pytest.approx(0.1)
    assert src.masses["A"] > src.masses["B"]
    total = sum(src.masses.values())
    assert abs(total - 1.0) < 1e-6


def test_from_ranked_results_empty_gives_unknown_source():
    src = from_ranked_results("empty", [])
    assert src.masses == {UNKNOWN: 1.0}


def test_from_ranked_results_deduplicates_ids():
    results = [
        {"id": "A", "score": 2.0},
        {"id": "A", "score": 1.0},
    ]
    src = from_ranked_results("dup", results)
    # Single entry accumulates
    assert "A" in src.masses
    # Only A and Θ
    assert set(src.masses.keys()) == {"A", UNKNOWN}
