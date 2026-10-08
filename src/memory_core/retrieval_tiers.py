"""Candidate generators of the recall tiers added in 14.8.0.

Each function takes the Store (for its connection and embedder) and returns
`(knowledge_id, score)` pairs, best first. `Recall._search_impl` in server.py
decides when a tier runs and fuses the lists; nothing here ranks across tiers.
"""

from __future__ import annotations

import math

RRF_K = 60

# Graph node types that link to nearly every record of a project and
# therefore carry no signal about which records belong together.
GRAPH_HUB_TYPES = ("project", "event", "session", "domain")
# Shared-entity walk: at most this many seed nodes, the most specific
# ones (fewest linked records) first. Bounds the join to
# GRAPH_MAX_SEED_NODES x MEMORY_GRAPH_HUB_DEGREE rows.
GRAPH_MAX_SEED_NODES = 16

def shared_entity_neighbours(store, seed_ids, seed_scores, *, limit):
    """Records that share an entity node with a seed record, via the knowledge graph.

    knowledge_nodes links records to graph nodes (auto_link on save, triple
    extraction in reflection). Two records linked to the same concept,
    technology or person node are neighbours. Hub nodes — project/event
    nodes and nodes linked to more than MEMORY_GRAPH_HUB_DEGREE records —
    are skipped: they would connect everything to everything.
    Score: 0.4 x seed score x link strength x node specificity, summed
    over shared nodes, where specificity = 1 / ln(1 + degree): a node
    linked to 3 records says more than one linked to 150. Only
    candidates within half of the best graph score are kept, at most
    `limit` of them.
    """
    seeds = [int(kid) for kid in seed_ids]
    if not seeds:
        return []
    from config import get_graph_hub_degree
    hub_degree = get_graph_hub_degree()
    marks = ",".join("?" * len(seeds))
    seed_links = store.db.execute(f"""
        SELECT kn.knowledge_id, kn.node_id, COALESCE(kn.strength, 1.0) AS strength
        FROM knowledge_nodes kn JOIN graph_nodes n ON n.id = kn.node_id
        WHERE kn.knowledge_id IN ({marks})
          AND n.type NOT IN ({",".join("?" * len(GRAPH_HUB_TYPES))})
          AND COALESCE(n.status, 'active') = 'active'
    """, [*seeds, *GRAPH_HUB_TYPES]).fetchall()
    if not seed_links:
        return []
    node_seed_weight: dict[str, float] = {}
    for row in seed_links:
        kid, node_id, strength = int(row[0]), row[1], float(row[2])
        weight = 0.4 * float(seed_scores.get(kid, 0.0)) * strength
        node_seed_weight[node_id] = max(node_seed_weight.get(node_id, 0.0), weight)
    node_ids = list(node_seed_weight)
    node_marks = ",".join("?" * len(node_ids))
    degrees = dict(store.db.execute(f"""
        SELECT node_id, COUNT(*) FROM knowledge_nodes WHERE node_id IN ({node_marks}) GROUP BY node_id
    """, node_ids).fetchall())
    # A node linked to one record (the seed itself) has no neighbours; hubs connect everything.
    kept = sorted((node_id for node_id in node_ids if 2 <= degrees.get(node_id, 0) <= hub_degree),
                  key=lambda node_id: (degrees[node_id], node_id))[:GRAPH_MAX_SEED_NODES]
    if not kept:
        return []
    specificity = {node_id: 1.0 / math.log1p(degrees[node_id]) for node_id in kept}
    kept_marks = ",".join("?" * len(kept))
    scores: dict[int, float] = {}
    for row in store.db.execute(f"""
        SELECT kn.knowledge_id, kn.node_id, COALESCE(kn.strength, 1.0)
        FROM knowledge_nodes kn JOIN knowledge k ON k.id = kn.knowledge_id
        WHERE kn.node_id IN ({kept_marks}) AND kn.knowledge_id NOT IN ({marks}) AND k.status = 'active'
    """, [*kept, *seeds]).fetchall():
        kid = int(row[0])
        scores[kid] = scores.get(kid, 0.0) + node_seed_weight[row[1]] * float(row[2]) * specificity[row[1]]
    if not scores:
        return []
    best = max(scores.values())
    ranked = sorted(((kid, score) for kid, score in scores.items() if score >= 0.5 * best),
                    key=lambda item: (-item[1], item[0]))
    return ranked[:limit]

def lexical_candidates(store, text, *, project, ktype, branch, limit):
    """Top `limit` FTS matches of `text` as (knowledge_id, normalised bm25) pairs."""
    from memory_core.fts_schema import SCOPED_BM25_WEIGHTS, scoped_match
    from memory_core.query_terms import fts_match_query
    match = fts_match_query(text)
    if not match:
        return []
    conds, params = ["k.status='active'"], []
    if ktype != "all":
        conds.append("k.type=?")
        params.append(ktype)
    if branch:
        conds.append("(k.branch=? OR k.branch='')")
        params.append(branch)
    if project:
        rows = store.db.execute(f"""
            WITH f AS MATERIALIZED (
                SELECT rowid AS id, bm25(knowledge_fts, {SCOPED_BM25_WEIGHTS}) AS r
                FROM knowledge_fts WHERE knowledge_fts MATCH ?
            )
            SELECT k.id, f.r FROM f JOIN knowledge k ON k.id = f.id
            WHERE {' AND '.join(conds)} ORDER BY f.r, k.id LIMIT ?
        """, [scoped_match(match, project), *params, limit]).fetchall()
    else:
        rows = store.db.execute(f"""
            SELECT k.id, bm25(knowledge_fts) AS r FROM knowledge_fts f JOIN knowledge k ON k.id = f.rowid
            WHERE knowledge_fts MATCH ? AND {' AND '.join(conds)} ORDER BY r, k.id LIMIT ?
        """, [match, *params, limit]).fetchall()
    top = max((abs(row[1]) for row in rows), default=0.0) or 1.0
    return [(int(row[0]), abs(row[1]) / top) for row in rows]

def directive_candidates(store, query, *, project, branch, limit, can_embed):
    """Convention records of the project that answer an advice-shaped query."""
    scores: dict[int, float] = {}
    for kid, score in lexical_candidates(store, query, project=project, ktype="convention",
                                               branch=branch, limit=limit):
        scores[kid] = max(scores.get(kid, 0.0), 0.5 * score)
    if can_embed:
        for kid, similarity in store._search_spaces(query, project=project, limit=limit,
                                                      kind="convention", branch=branch):
            scores[int(kid)] = max(scores.get(int(kid), 0.0), max(0.0, float(similarity)))
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]

def multi_query_candidates(store, parts, *, fuse, project, ktype, branch, limit, can_embed, spaces):
    """Fuse the FTS and semantic results of each sub-query with RRF; score = fused RRF."""
    rankings: dict[str, list[int]] = {}
    for index, part in enumerate(parts):
        lexical = [kid for kid, _ in lexical_candidates(store, part, project=project, ktype=ktype,
                                                              branch=branch, limit=limit)]
        if lexical:
            rankings[f"fts:{index}"] = lexical
        if can_embed:
            semantic = store._search_spaces(part, project=project, spaces=spaces, limit=limit,
                                             kind=ktype, branch=branch)
            semantic.sort(key=lambda pair: pair[1], reverse=True)
            if semantic:
                rankings[f"semantic:{index}"] = [int(kid) for kid, _ in semantic]
    if not rankings:
        return []
    fused = fuse(rankings, {name: 1.0 for name in rankings}, RRF_K)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))[:limit * 2]
