"""
Dempster-Shafer evidence fusion — v7.0 Phase H.

Combines mass functions from multiple sources of evidence (e.g. internal
memory hits vs external research result) into a single belief distribution.

The classical combination rule:

    m12(A) = (1 / (1 - K)) * Σ_{B∩C=A} m1(B)·m2(C)     for A ≠ ∅
    K = Σ_{B∩C=∅} m1(B)·m2(C)                          (conflict mass)

We model hypotheses as plain strings and always include Θ ("unknown") as
the universal set. Frame of discernment is implicit: any non-Θ hypothesis
is pairwise disjoint. This is the standard "Bayesian flattening" common in
practical DS applications.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UNKNOWN = "Θ"  # universal / unknown hypothesis


@dataclass
class EvidenceSource:
    """A single mass function over hypotheses. Must sum to 1 (within eps)."""
    name: str
    masses: dict[str, float]

    def normalised(self) -> dict[str, float]:
        total = sum(self.masses.values())
        if total <= 0:
            return {UNKNOWN: 1.0}
        return {k: v / total for k, v in self.masses.items()}


def _combine_two(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """Dempster's combination of two mass functions treating non-Θ hypotheses
    as mutually exclusive."""
    out: dict[str, float] = {}
    conflict = 0.0

    for ka, va in a.items():
        for kb, vb in b.items():
            product = va * vb
            if ka == kb or ka == UNKNOWN or kb == UNKNOWN:
                # Intersection is the more specific hypothesis (Θ acts as neutral)
                key = kb if ka == UNKNOWN else ka
                out[key] = out.get(key, 0.0) + product
            else:
                # Different specific hypotheses → conflict
                conflict += product

    if conflict >= 1.0 - 1e-12:
        # Total conflict → fall back to prior (unknown)
        return {UNKNOWN: 1.0}

    norm = 1.0 - conflict
    return {k: v / norm for k, v in out.items()}


def combine(sources: list[EvidenceSource]) -> dict[str, Any]:
    """Fuse an arbitrary number of sources.

    Returns {
      'masses': {hypothesis: mass, ...},
      'best_hypothesis': str,
      'best_mass': float,
      'conflict': float   # total conflict consumed in combination
    }
    """
    if not sources:
        return {"masses": {UNKNOWN: 1.0}, "best_hypothesis": UNKNOWN,
                "best_mass": 1.0, "conflict": 0.0}

    acc = sources[0].normalised()
    total_conflict = 0.0
    for src in sources[1:]:
        before = {**acc}
        nxt = src.normalised()
        acc = _combine_two(acc, nxt)
        # Measure conflict for this step
        c = 0.0
        for ka, va in before.items():
            for kb, vb in nxt.items():
                if ka != kb and ka != UNKNOWN and kb != UNKNOWN:
                    c += va * vb
        total_conflict += c

    # Pick best specific hypothesis; fall back to Θ if none
    specific = {k: v for k, v in acc.items() if k != UNKNOWN}
    if specific:
        best = max(specific, key=specific.get)
        best_mass = specific[best]
    else:
        best = UNKNOWN
        best_mass = acc.get(UNKNOWN, 1.0)

    return {
        "masses": acc,
        "best_hypothesis": best,
        "best_mass": round(best_mass, 6),
        "conflict": round(total_conflict, 6),
    }


# ──────────────────────────────────────────────
# High-level helper: fuse ranked result lists into hypothesis masses
# ──────────────────────────────────────────────

def from_ranked_results(
    name: str,
    results: list[dict[str, Any]],
    *,
    key: str = "id",
    score_key: str = "score",
    unknown_mass: float = 0.1,
) -> EvidenceSource:
    """Convert a ranked list of results (from recall/search) into a DS source.

    Reserves `unknown_mass` for Θ so fusion remains robust against total
    conflict. The remaining 1 - unknown_mass is distributed proportionally
    to scores.
    """
    if not results:
        return EvidenceSource(name=name, masses={UNKNOWN: 1.0})
    scores = [max(0.0, float(r.get(score_key, 0.0))) for r in results]
    total = sum(scores)
    if total <= 0:
        return EvidenceSource(name=name, masses={UNKNOWN: 1.0})
    masses: dict[str, float] = {UNKNOWN: unknown_mass}
    remaining = 1.0 - unknown_mass
    for r, s in zip(results, scores):
        k = str(r.get(key, id(r)))
        masses[k] = masses.get(k, 0.0) + remaining * (s / total)
    return EvidenceSource(name=name, masses=masses)
