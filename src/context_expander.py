"""Context Expander — graph-based 1-hop expansion of retrieval results.

Given a list of seed knowledge records (e.g. top-k from memory_recall), find
semantically related knowledge via the KG:
 1. Collect concept/entity nodes linked to seeds (via knowledge_nodes).
 2. Walk 1-hop in graph_edges to neighboring nodes.
 3. Collect knowledge records linked to those neighbor nodes.
 4. Rank by co-occurrence overlap (how many seed-related nodes a candidate
    shares) and link strength.
 5. Return top-N, seeds excluded, archived filtered.

Implements the "context expansion" idea from the screenshots: don't just
return top-k by similarity — include contextually related records so the
agent sees the full picture.
"""

from __future__ import annotations

import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

try:
    from config import get_edge_half_life_days
except ImportError:  # pragma: no cover — fallback when imported as package
    def get_edge_half_life_days() -> int:  # type: ignore[no-redef]
        return 60

LOG = lambda msg: sys.stderr.write(f"[context-expander] {msg}\n")


def _edge_freshness(ts_str: str | None, half_life_days: int) -> float:
    """Exponential decay on edge age. Falls back to 0.5 when ts is missing.

    Newly created / recently reinforced edges keep contribution near 1.0;
    edges older than ``half_life_days`` carry less than half their nominal
    weight. Clamped to [0.05, 1.0] so a very old edge still nudges (we don't
    want it dominating, but it's not zero-evidence either).
    """
    if not ts_str:
        return 0.5
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc) if ts.tzinfo else datetime.utcnow()
        days = max(0.0, (now - ts).total_seconds() / 86400.0)
        return max(0.05, min(1.0, math.exp(-days * math.log(2) / max(1, half_life_days))))
    except Exception:
        return 0.5


class ContextExpander:
    """Expand a set of seed knowledge_ids via 1-hop KG traversal."""

    def __init__(self, db: sqlite3.Connection, edge_half_life_days: int | None = None) -> None:
        self.db = db
        self._edge_half_life = (
            edge_half_life_days
            if edge_half_life_days is not None
            else get_edge_half_life_days()
        )

    def expand(
        self,
        seed_ids: list[int],
        budget: int = 5,
        depth: int = 1,
        min_strength: float = 0.0,
    ) -> list[int]:
        """Return up to `budget` knowledge_ids related to seeds.

        Ranking: for each candidate, a score accumulates from every seed-linked
        node it shares (direct or 1-hop neighbor), weighted by edge weight,
        link strength, and **edge freshness**. Stale 1-hop edges contribute
        less than recently reinforced ones, so fresh evidence can rescue
        otherwise dormant nodes. Seeds themselves are never included.
        """
        if not seed_ids:
            return []

        seed_set = set(seed_ids)

        # Step 1: nodes linked to seeds (+ the link strength to seed).
        seed_nodes: dict[str, float] = self._nodes_of(seed_ids, min_strength)
        if not seed_nodes:
            return []

        # Step 2: 1-hop neighbors via graph_edges
        # (weight × freshness as multiplier).
        expanded_nodes: dict[str, float] = dict(seed_nodes)
        if depth >= 1:
            for node_id, seed_strength in seed_nodes.items():
                for neighbor_id, edge_weight, freshness in self._one_hop(node_id):
                    contribution = seed_strength * edge_weight * freshness
                    expanded_nodes[neighbor_id] = max(
                        expanded_nodes.get(neighbor_id, 0.0), contribution
                    )

        # Step 3 & 4: candidates via knowledge_nodes back-link, scored.
        scores: dict[int, float] = defaultdict(float)
        if not expanded_nodes:
            return []

        placeholders = ",".join("?" * len(expanded_nodes))
        rows = self.db.execute(
            f"""SELECT kn.knowledge_id, kn.node_id, kn.strength
                  FROM knowledge_nodes kn
                  JOIN knowledge k ON k.id = kn.knowledge_id
                 WHERE kn.node_id IN ({placeholders})
                   AND k.status = 'active'
                   AND k.superseded_by IS NULL""",
            list(expanded_nodes.keys()),
        ).fetchall()

        for r in rows:
            kid = r["knowledge_id"]
            if kid in seed_set:
                continue
            node_weight = expanded_nodes.get(r["node_id"], 0.0)
            link_strength = float(r["strength"] or 1.0)
            scores[kid] += node_weight * link_strength

        if not scores:
            return []

        # Sort: score desc, then id asc (deterministic tiebreak)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [kid for kid, _ in ranked[:budget]]

    # ──────────────────────────────────────────────
    # Internals
    # ──────────────────────────────────────────────

    def _nodes_of(self, ids: list[int], min_strength: float) -> dict[str, float]:
        """Return {node_id: max_link_strength} for nodes linked to any seed."""
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"""SELECT node_id, MAX(strength) AS s
                  FROM knowledge_nodes
                 WHERE knowledge_id IN ({placeholders})
                 GROUP BY node_id""",
            ids,
        ).fetchall()
        return {
            r["node_id"]: float(r["s"] or 1.0)
            for r in rows
            if (r["s"] or 0.0) >= min_strength
        }

    def _one_hop(self, node_id: str) -> list[tuple[str, float, float]]:
        """Return [(neighbor_id, edge_weight, freshness), ...] for 1-hop neighbors.

        ``freshness`` is exponential decay on ``last_reinforced_at`` (falling
        back to ``created_at`` when never reinforced). Half-life is set on
        the expander instance (env: ``MEMORY_EDGE_HALF_LIFE_DAYS``).
        """
        rows = self.db.execute(
            """SELECT source_id, target_id, weight,
                      last_reinforced_at, created_at
                 FROM graph_edges
                WHERE source_id = ? OR target_id = ?""",
            (node_id, node_id),
        ).fetchall()
        out: list[tuple[str, float, float]] = []
        hl = self._edge_half_life
        for r in rows:
            w = float(r["weight"] or 1.0)
            neighbor = r["target_id"] if r["source_id"] == node_id else r["source_id"]
            if not neighbor or neighbor == node_id:
                continue
            # Normalize weight to [0, 1] — v5 edges store weights up to 10.0
            norm_w = min(w / 10.0, 1.0) if w > 1.0 else w
            ts = r["last_reinforced_at"] or r["created_at"]
            freshness = _edge_freshness(ts, hl)
            out.append((neighbor, norm_w, freshness))
        return out
