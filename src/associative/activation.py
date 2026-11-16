"""
Spreading Activation — brain-like activation through the knowledge graph.

Simulates neural spreading activation: seed nodes fire, activation propagates
through edges with distance-based decay, and multi-path convergence amplifies
signal. This replaces keyword matching with resonance-based memory retrieval.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict

from memory_core.retrieval import SearchScope

LOG = lambda msg: sys.stderr.write(f"[memory-activation] {msg}\n")

# Frontier nodes read per hop, most active first.
MAX_FRONTIER_NODES = 200
# Activated nodes joined to knowledge links; two bind parameters each, far
# below SQLite's variable limit.
MAX_LINKED_NODES = 2000
# Ids per edge query (bound twice, once per direction).
EDGE_QUERY_CHUNK = 400


class SpreadingActivation:
    """Brain-like spreading activation through the knowledge graph."""

    # Decay factor per hop distance (index = hop count)
    HOP_DECAY: list[float] = [1.0, 0.7, 0.4, 0.2]

    # Minimum activation threshold to keep a node in the result
    ACTIVATION_THRESHOLD: float = 0.3

    # Bonus multiplier when multiple paths converge on the same node
    MULTI_PATH_BONUS: dict[int, float] = {2: 1.2, 3: 1.5, 4: 1.8, 5: 2.0}

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def spread(
        self, seed_nodes: list[str], depth: int = 2
    ) -> dict[str, float]:
        """Spreading activation from seed nodes through the graph.

        Args:
            seed_nodes: List of graph_node IDs to start activation from.
            depth: How many hops to spread (1-3). Clamped to valid range.

        Returns:
            Dict of node_id to activation_level, containing only nodes
            whose activation exceeds ACTIVATION_THRESHOLD.
        """
        if not seed_nodes:
            return {}

        depth = max(1, min(depth, len(self.HOP_DECAY) - 1))

        # activation_level[node_id] = max activation seen
        activation_level: dict[str, float] = {}
        # path_count[node_id] = number of distinct paths reaching this node
        path_count: dict[str, int] = defaultdict(int)

        # Seed nodes get full activation
        for node_id in seed_nodes:
            activation_level[node_id] = 1.0
            path_count[node_id] = 1

        # BFS-like spreading with decay
        current_frontier: set[str] = set(seed_nodes)

        for hop in range(1, depth + 1):
            decay = self.HOP_DECAY[hop]
            next_frontier: set[str] = set()
            active = sorted(
                (n for n in current_frontier if activation_level.get(n, 0.0) >= self.ACTIVATION_THRESHOLD),
                key=lambda n: (-activation_level[n], n),
            )[:MAX_FRONTIER_NODES]
            adjacency = self._neighbors(active)

            for node_id in active:
                parent_activation = activation_level[node_id]

                # Spread to all neighbors
                neighbors = adjacency.get(node_id, [])
                for neighbor_id, edge_weight in neighbors:
                    # Skip seeds to avoid trivial cycles back to origin
                    if neighbor_id in seed_nodes and hop > 0:
                        continue

                    # Activation = parent * decay * edge_weight (normalized)
                    new_activation = parent_activation * decay * min(edge_weight, 1.0)

                    if new_activation < self.ACTIVATION_THRESHOLD:
                        continue

                    path_count[neighbor_id] += 1

                    # Keep the maximum activation from any path
                    if neighbor_id not in activation_level or new_activation > activation_level[neighbor_id]:
                        activation_level[neighbor_id] = new_activation
                        next_frontier.add(neighbor_id)

            current_frontier = next_frontier

        # Apply multi-path bonus: nodes reached by multiple paths get amplified
        for node_id, count in path_count.items():
            if count >= 2 and node_id not in seed_nodes:
                clamped = min(count, 5)
                bonus = self.MULTI_PATH_BONUS.get(clamped, 2.0)
                activation_level[node_id] = min(
                    activation_level[node_id] * bonus, 1.0
                )

        # Filter below threshold and exclude seed nodes from output
        result = {
            nid: round(level, 4)
            for nid, level in activation_level.items()
            if level >= self.ACTIVATION_THRESHOLD and nid not in seed_nodes
        }

        LOG(
            f"spread: {len(seed_nodes)} seeds, depth={depth}, "
            f"activated={len(result)} nodes"
        )
        return result

    def find_seed_nodes(self, concept_names: list[str]) -> list[str]:
        """Find graph node IDs matching concept names (case-insensitive).

        Searches graph_nodes by exact name match (case-insensitive) first,
        then falls back to a prefix match for partial names. Both use the
        indexed `name_norm` column; its value is `lower(trim(name))`, set by
        the migration 026 triggers, so the query applies the same function.

        Args:
            concept_names: List of concept name strings to search for.

        Returns:
            List of matching node IDs (deduplicated).
        """
        if not concept_names:
            return []

        found_ids: list[str] = []
        seen: set[str] = set()

        for name in concept_names:
            # Exact match first (case-insensitive)
            row = self.db.execute(
                "SELECT id FROM graph_nodes WHERE name_norm = lower(trim(?)) AND status = 'active'",
                (name,),
            ).fetchone()

            if row:
                nid = row[0]
                if nid not in seen:
                    found_ids.append(nid)
                    seen.add(nid)
                continue

            # Fallback: prefix match, as an index range on name_norm. The
            # unary + keeps the planner off the status index, which it
            # otherwise prefers and which scans every active node.
            rows = self.db.execute(
                "SELECT id FROM graph_nodes WHERE name_norm >= lower(trim(?1)) "
                "AND name_norm < lower(trim(?1)) || char(1114111) AND +status = 'active' LIMIT 3",
                (name,),
            ).fetchall()

            for r in rows:
                nid = r[0]
                if nid not in seen:
                    found_ids.append(nid)
                    seen.add(nid)

        LOG(f"find_seed_nodes: {len(concept_names)} concepts -> {len(found_ids)} seeds")
        return found_ids

    def get_activated_memories(
        self, activation_map: dict[str, float], top_k: int = 20,
        scope: SearchScope | None = None,
    ) -> list[tuple[int, float]]:
        """Find knowledge records connected to activated nodes.

        Looks up knowledge_nodes links for all activated graph nodes and
        sums activation scores across all paths to each knowledge record.

        Args:
            activation_map: Dict of node_id to activation_level from spread().
            top_k: Maximum number of knowledge records to return.

        Returns:
            List of (knowledge_id, total_activation_score) sorted by score
            descending. Score is the sum of (activation * link_strength)
            across all connected nodes.
        """
        if not activation_map:
            return []

        # Sum in SQL: hub nodes (a tag or project) link thousands of records,
        # and shipping every link to Python took seconds on a real store.
        # A missing or zero strength counts as 1.0.
        if len(activation_map) > MAX_LINKED_NODES:
            strongest = sorted(activation_map.items(), key=lambda item: (-item[1], item[0]))[:MAX_LINKED_NODES]
            activation_map = dict(strongest)
        values = ",".join("(?,?)" for _ in activation_map)
        params: list = [x for item in activation_map.items() for x in item]
        predicates, scope_params = (scope or SearchScope()).sql()
        rows = self.db.execute(
            f"""WITH a(node_id, act) AS (VALUES {values})
                SELECT kn.knowledge_id,
                       SUM(a.act * CASE WHEN kn.strength IS NULL OR kn.strength = 0
                                        THEN 1.0 ELSE kn.strength END) AS score
                FROM a
                JOIN knowledge_nodes kn ON kn.node_id = a.node_id
                JOIN knowledge k ON k.id = kn.knowledge_id
                WHERE {' AND '.join(predicates)}
                GROUP BY kn.knowledge_id
                ORDER BY score DESC, kn.knowledge_id
                LIMIT ?""",
            [*params, *scope_params, top_k],
        ).fetchall()
        result = [(row[0], round(row[1], 4)) for row in rows]

        LOG(f"get_activated_memories: {len(activation_map)} nodes -> {len(result)} memories")
        return result

    def _neighbors(self, node_ids: list[str]) -> dict[str, list[tuple[str, float]]]:
        """Edges touching `node_ids`, both directions, with normalised weights.

        Reads only the frontier's edges through the source/target indexes;
        loading every edge of the graph on each recall took 300 ms on a real
        store and grows with every save.

        Returns:
            Dict mapping node_id to list of (neighbor_id, weight) tuples.
        """
        from config import get_graph_hub_degree
        from memory_core.retrieval_tiers import GRAPH_HUB_TYPES

        adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
        if not node_ids:
            return adjacency
        hub_degree = get_graph_hub_degree()
        edges: dict[str, list[tuple[str, float, str | None]]] = defaultdict(list)
        for offset in range(0, len(node_ids), EDGE_QUERY_CHUNK):
            chunk = node_ids[offset:offset + EDGE_QUERY_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = self.db.execute(
                f"""SELECT e.source_id, e.target_id, e.weight, n.type FROM graph_edges e
                    LEFT JOIN graph_nodes n ON n.id = e.target_id WHERE e.source_id IN ({placeholders})
                    UNION ALL
                    SELECT e.target_id, e.source_id, e.weight, n.type FROM graph_edges e
                    LEFT JOIN graph_nodes n ON n.id = e.source_id WHERE e.target_id IN ({placeholders})""",
                [*chunk, *chunk],
            ).fetchall()
            for node, neighbor, weight, neighbor_type in rows:
                edges[node].append((neighbor, weight, neighbor_type))
        for node, links in edges.items():
            # A node linked to more than hub_degree others is a tag or a project: it connects everything.
            if len(links) > hub_degree:
                continue
            for neighbor, weight, neighbor_type in links:
                if neighbor_type in GRAPH_HUB_TYPES:
                    continue
                weight = weight if weight else 1.0
                # Normalize weight to [0, 1] range (edges can have weight up to 10.0)
                adjacency[node].append((neighbor, min(weight / 10.0, 1.0) if weight > 1.0 else weight))
        return adjacency
