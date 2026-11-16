"""Candidate generators of the recall tiers added in 14.8.0.

Each function takes the Store (for its connection and embedder) and returns
`(knowledge_id, score)` pairs, best first. `Recall._search_impl` in server.py
decides when a tier runs and fuses the lists; nothing here ranks across tiers.
"""

from __future__ import annotations

import math

from memory_core.retrieval import SearchScope
from memory_core.telemetry import op_timer

RRF_K = 60

# Graph node types that link to nearly every record of a project and
# therefore carry no signal about which records belong together.
GRAPH_HUB_TYPES = ("project", "event", "session", "domain")
# Shared-entity walk: at most this many seed nodes, the most specific
# ones (fewest linked records) first. Bounds the join to
# GRAPH_MAX_SEED_NODES x MEMORY_GRAPH_HUB_DEGREE rows.
GRAPH_MAX_SEED_NODES = 16

@op_timer("retrieval_graph_neighbours_ms")
def shared_entity_neighbours(store, seed_ids, seed_scores, *, limit, scope: SearchScope | None = None):
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
    count_conds, count_params = scope.sql() if scope is not None else (["1=1"], [])
    degrees = dict(store.db.execute(f"""
        SELECT kn.node_id, COUNT(*) FROM knowledge_nodes kn JOIN knowledge k ON k.id=kn.knowledge_id
        WHERE kn.node_id IN ({node_marks}) AND {' AND '.join(count_conds)} GROUP BY kn.node_id
    """, [*node_ids, *count_params]).fetchall())
    # A node linked to one record (the seed itself) has no neighbours; hubs connect everything.
    kept = sorted((node_id for node_id in node_ids if 2 <= degrees.get(node_id, 0) <= hub_degree),
                  key=lambda node_id: (degrees[node_id], node_id))[:GRAPH_MAX_SEED_NODES]
    if not kept:
        return []
    specificity = {node_id: 1.0 / math.log1p(degrees[node_id]) for node_id in kept}
    kept_marks = ",".join("?" * len(kept))
    scores: dict[int, float] = {}
    conds, params = scope.sql() if scope is not None else (["k.status='active'"], [])
    for row in store.db.execute(f"""
        SELECT kn.knowledge_id, kn.node_id, COALESCE(kn.strength, 1.0)
        FROM knowledge_nodes kn JOIN knowledge k ON k.id = kn.knowledge_id
        WHERE kn.node_id IN ({kept_marks}) AND kn.knowledge_id NOT IN ({marks}) AND {' AND '.join(conds)}
    """, [*kept, *seeds, *params]).fetchall():
        kid = int(row[0])
        scores[kid] = scores.get(kid, 0.0) + node_seed_weight[row[1]] * float(row[2]) * specificity[row[1]]
    if not scores:
        return []
    best = max(scores.values())
    ranked = sorted(((kid, score) for kid, score in scores.items() if score >= 0.5 * best),
                    key=lambda item: (-item[1], item[0]))
    return ranked[:limit]

# Query-entity tier: at most this many name variants are looked up, and at most
# QUERY_ENTITY_MAX_NODES matched nodes (the most specific first) are expanded.
QUERY_ENTITY_MAX_NAMES = 64
QUERY_ENTITY_MAX_NODES = 16
QUERY_ENTITY_MIN_NAME_CHARS = 3
# Each further matched entity linked to the same record adds this share of the score.
QUERY_ENTITY_MULTI_BONUS = 0.5
# IDF = 1 / ln(QUERY_ENTITY_IDF_OFFSET + mentions); 2 keeps a single mention below 1 / ln(2).
QUERY_ENTITY_IDF_OFFSET = 2.0


def query_entity_names(query: str) -> list[str]:
    """Name variants of the query's possible entities, for an exact `graph_nodes.name` lookup.

    Capitalised / identifier-shaped tokens (query_router.extract_entity_candidates),
    the query's content words and adjacent word pairs, each as typed, lower
    case and capitalised (graph names are stored in either form).
    """
    from memory_core.query_terms import lexical_terms
    from query_router import extract_entity_candidates

    words = [term for term in lexical_terms(query) if len(term) >= QUERY_ENTITY_MIN_NAME_CHARS]
    phrases = [*extract_entity_candidates(query), *words,
               *(f"{first} {second}" for first, second in zip(words, words[1:]))]
    names: dict[str, None] = {}
    for phrase in phrases:
        for variant in (phrase, phrase.lower(), phrase[:1].upper() + phrase[1:].lower()):
            if len(variant) >= QUERY_ENTITY_MIN_NAME_CHARS:
                names.setdefault(variant, None)
    return list(names)[:QUERY_ENTITY_MAX_NAMES]


@op_timer("retrieval_query_entities_ms")
def query_entity_candidates(store, query, *, project, ktype, branch, limit, scope: SearchScope | None = None):
    """Records linked to graph entities named in the query, as (knowledge_id, score) pairs.

    Each matched entity node (not a hub type, linked to at most
    MEMORY_GRAPH_HUB_DEGREE records) weighs IDF = 1 / ln(2 + mentions), where
    mentions is the number of records linked to it. A record's weight is the
    sum of the IDFs of the matched entities it is linked to, times
    1 + 0.5 x (N - 1) for N such entities, so a record naming two of the
    query's entities outranks one naming a single entity. Score =
    weight / (1 + weight), in (0, 1).
    """
    names = query_entity_names(query)
    if not names:
        return []
    from config import get_graph_hub_degree
    hub_degree = get_graph_hub_degree()
    nodes = [row[0] for row in store.db.execute(f"""
        SELECT id FROM graph_nodes
        WHERE name IN ({",".join("?" * len(names))})
          AND type NOT IN ({",".join("?" * len(GRAPH_HUB_TYPES))})
          AND COALESCE(status, 'active') = 'active'
    """, [*names, *GRAPH_HUB_TYPES]).fetchall()]
    if not nodes:
        return []
    count_conds, count_params = scope.sql() if scope is not None else (["1=1"], [])
    mentions = dict(store.db.execute(f"""
        SELECT kn.node_id, COUNT(*) FROM knowledge_nodes kn JOIN knowledge k ON k.id=kn.knowledge_id
        WHERE kn.node_id IN ({",".join("?" * len(nodes))}) AND {' AND '.join(count_conds)}
        GROUP BY kn.node_id
    """, [*nodes, *count_params]).fetchall())
    kept = sorted((node for node in dict.fromkeys(nodes) if 1 <= mentions.get(node, 0) <= hub_degree),
                  key=lambda node: (mentions[node], node))[:QUERY_ENTITY_MAX_NODES]
    if not kept:
        return []
    idf = {node: 1.0 / math.log(QUERY_ENTITY_IDF_OFFSET + mentions[node]) for node in kept}
    conds, params = ["k.status='active'"], []
    if project:
        conds.append("k.project=?")
        params.append(project)
    if ktype != "all":
        conds.append("k.type=?")
        params.append(ktype)
    if branch:
        conds.append("(k.branch=? OR k.branch='')")
        params.append(branch)
    if scope is not None:
        conds, params = scope.sql()
    linked: dict[int, set[str]] = {}
    for row in store.db.execute(f"""
        SELECT DISTINCT kn.knowledge_id, kn.node_id FROM knowledge_nodes kn JOIN knowledge k ON k.id = kn.knowledge_id
        WHERE kn.node_id IN ({",".join("?" * len(kept))}) AND {" AND ".join(conds)}
    """, [*kept, *params]).fetchall():
        linked.setdefault(int(row[0]), set()).add(row[1])
    scored = []
    for kid, entity_nodes in linked.items():
        weight = sum(idf[node] for node in entity_nodes) * (1.0 + QUERY_ENTITY_MULTI_BONUS * (len(entity_nodes) - 1))
        scored.append((kid, weight / (1.0 + weight)))
    # Ties: the newer record first.
    scored.sort(key=lambda item: (-item[1], -item[0]))
    return scored[:limit]


@op_timer("retrieval_lexical_ms")
def lexical_candidates(store, text, *, project, ktype, branch, limit, scope: SearchScope | None = None):
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
    if scope is not None:
        conds, params = scope.sql()
    if store.is_postgres:
        from memory_core.pg_fts import match_source
        weights = tuple(float(weight) for weight in SCOPED_BM25_WEIGHTS.split(",")) if project else ()
        source, source_params = match_source("knowledge_fts", scoped_match(match, project) if project else match,
                                             weights=weights)
        rows = store.db.execute(f"""
            SELECT k.id, f.rank AS r FROM ({source}) f JOIN knowledge k ON k.id=f.id
            WHERE {' AND '.join(conds)} ORDER BY f.rank, k.id LIMIT ?
        """, [*source_params, *params, limit]).fetchall()
    elif project:
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

@op_timer("retrieval_directives_ms")
def directive_candidates(store, query, *, project, branch, limit, can_embed, scope: SearchScope | None = None):
    """Convention records of the project that answer an advice-shaped query."""
    scores: dict[int, float] = {}
    from dataclasses import replace
    scope_kwargs = {"scope": replace(scope, kind="convention")} if scope is not None else {}
    for kid, score in lexical_candidates(store, query, project=project, ktype="convention",
                                               branch=branch, limit=limit, **scope_kwargs):
        scores[kid] = max(scores.get(kid, 0.0), 0.5 * score)
    if can_embed:
        for kid, similarity in store._search_spaces(query, project=project, limit=limit,
                                                      kind="convention", branch=branch, **scope_kwargs):
            scores[int(kid)] = max(scores.get(int(kid), 0.0), max(0.0, float(similarity)))
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]

@op_timer("retrieval_multi_query_ms")
def multi_query_candidates(store, parts, *, fuse, project, ktype, branch, limit, can_embed, spaces,
                           scope: SearchScope | None = None):
    """Fuse the FTS and semantic results of each sub-query with RRF; score = fused RRF."""
    rankings: dict[str, list[int]] = {}
    scope_kwargs = {"scope": scope} if scope is not None else {}
    for index, part in enumerate(parts):
        lexical = [kid for kid, _ in lexical_candidates(store, part, project=project, ktype=ktype,
                                                              branch=branch, limit=limit, **scope_kwargs)]
        if lexical:
            rankings[f"fts:{index}"] = lexical
        if can_embed:
            semantic = store._search_spaces(part, project=project, spaces=spaces, limit=limit,
                                             kind=ktype, branch=branch, **scope_kwargs)
            semantic.sort(key=lambda pair: pair[1], reverse=True)
            if semantic:
                rankings[f"semantic:{index}"] = [int(kid) for kid, _ in semantic]
    if not rankings:
        return []
    fused = fuse(rankings, {name: 1.0 for name in rankings}, RRF_K)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))[:limit * 2]


# Candidates per tier and part when looking for each compound part's best record.
COMPOUND_PART_POOL = 10


@op_timer("retrieval_compound_ms")
def compound_part_winners(store, parts, *, fuse, project, ktype, branch, can_embed, spaces,
                          scope: SearchScope | None = None):
    """The best record of each part of a compound query (FTS + semantic fused per part), in part order.

    A part with no candidate is skipped; a record that wins two parts is listed once.
    """
    winners: list[int] = []
    for part in parts:
        ranked = multi_query_candidates(store, [part], fuse=fuse, project=project, ktype=ktype, branch=branch,
                                        limit=COMPOUND_PART_POOL, can_embed=can_embed, spaces=spaces,
                                        **({"scope": scope} if scope is not None else {}))
        if ranked and ranked[0][0] not in winners:
            winners.append(int(ranked[0][0]))
    return winners


def guarantee_slots(ranked: list[int], winners: list[int], k: int) -> list[int]:
    """`ranked` ids with every winner inside the first `k`.

    A winner already in the top `k` stays where it is. Missing winners take
    the bottom places of the top `k`, in part order, displacing the
    lowest-ranked non-winners, which move just below the top `k` in their
    order. Ids are unique in the result.
    """
    if k <= 0 or not winners:
        return list(ranked)
    head = list(ranked[:k])
    winner_set = set(winners)
    missing = [kid for kid in winners if kid not in head]
    if not missing:
        return list(ranked)
    free = max(0, len(head) + len(missing) - k)
    non_winner_places = [index for index in range(len(head)) if head[index] not in winner_set]
    freed = set(non_winner_places[len(non_winner_places) - min(free, len(non_winner_places)):]) if free else set()
    kept = [kid for index, kid in enumerate(head) if index not in freed]
    displaced = [head[index] for index in sorted(freed)]
    new_head = (kept + missing)[:k]
    placed = set(new_head)
    tail = [kid for kid in ranked[k:] if kid not in placed]
    return new_head + displaced + tail
