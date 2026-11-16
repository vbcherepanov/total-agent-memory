"""Tests for ContextExpander — 1-hop graph expansion of retrieval results."""

from __future__ import annotations

import pytest


def _add_k(db, content: str, project: str = "demo") -> int:
    return db.execute(
        "INSERT INTO knowledge (content, project, status, created_at) "
        "VALUES (?, ?, 'active', ?)",
        (content, project, "2026-04-14T00:00:00Z"),
    ).lastrowid


def _link(db, knowledge_id: int, node_id: str, role: str = "mentions", strength: float = 1.0):
    db.execute(
        "INSERT OR REPLACE INTO knowledge_nodes (knowledge_id, node_id, role, strength) "
        "VALUES (?, ?, ?, ?)",
        (knowledge_id, node_id, role, strength),
    )
    db.commit()


# ──────────────────────────────────────────────
# Basic expansion
# ──────────────────────────────────────────────


def test_expand_returns_linked_knowledge_via_shared_nodes(db, graph_store):
    from context_expander import ContextExpander

    go = graph_store.add_node("technology", "go")
    k1 = _add_k(db, "fact about Go lang")
    k2 = _add_k(db, "another Go fact")
    _link(db, k1, go)
    _link(db, k2, go)

    ex = ContextExpander(db)
    result = ex.expand(seed_ids=[k1], budget=5)

    assert k2 in result
    assert k1 not in result  # seeds excluded


def test_expand_includes_one_hop_neighbors(db, graph_store):
    from context_expander import ContextExpander

    auth = graph_store.add_node("concept", "auth")
    jwt = graph_store.add_node("concept", "jwt")
    graph_store.add_edge(auth, jwt, "uses", weight=0.9)

    k_seed = _add_k(db, "auth overview")
    k_related = _add_k(db, "jwt deep dive")
    _link(db, k_seed, auth)
    _link(db, k_related, jwt)

    ex = ContextExpander(db)
    result = ex.expand(seed_ids=[k_seed], budget=5, depth=1)

    assert k_related in result


def test_expand_respects_budget(db, graph_store):
    from context_expander import ContextExpander

    hub = graph_store.add_node("concept", "hub")
    seed = _add_k(db, "seed record")
    _link(db, seed, hub)

    for i in range(10):
        kid = _add_k(db, f"linked record {i}")
        _link(db, kid, hub)

    ex = ContextExpander(db)
    result = ex.expand(seed_ids=[seed], budget=3)
    assert len(result) == 3


def test_expand_filters_archived(db, graph_store):
    from context_expander import ContextExpander

    node = graph_store.add_node("concept", "x")
    active = _add_k(db, "active record")
    archived = _add_k(db, "old record")
    _link(db, active, node)
    _link(db, archived, node)

    db.execute("UPDATE knowledge SET status='archived' WHERE id=?", (archived,))
    db.commit()

    seed = _add_k(db, "seed")
    _link(db, seed, node)

    ex = ContextExpander(db)
    result = ex.expand(seed_ids=[seed], budget=5)

    assert active in result
    assert archived not in result


def test_expand_empty_seeds_returns_empty(db):
    from context_expander import ContextExpander

    ex = ContextExpander(db)
    assert ex.expand(seed_ids=[], budget=5) == []


def test_expand_ranks_by_overlap_count(db, graph_store):
    """A candidate linked to multiple seed-related nodes should rank higher."""
    from context_expander import ContextExpander

    a = graph_store.add_node("concept", "a")
    b = graph_store.add_node("concept", "b")

    seed = _add_k(db, "seed")
    _link(db, seed, a)
    _link(db, seed, b)

    cand1 = _add_k(db, "only a")
    cand2 = _add_k(db, "both a and b")
    _link(db, cand1, a)
    _link(db, cand2, a)
    _link(db, cand2, b)

    ex = ContextExpander(db)
    result = ex.expand(seed_ids=[seed], budget=2)
    assert result[0] == cand2
    assert cand1 in result


@pytest.mark.parametrize('restricted', ['project', 'branch', 'tags', 'type'])
def test_expansion_filters_before_budget(db, graph_store, restricted):
    from context_expander import ContextExpander
    from memory_core.retrieval import SearchScope

    hub = graph_store.add_node('concept', 'ledger')
    seed = _add_k(db, 'Alpha invoices', 'alpha')
    hidden = _add_k(db, 'Restricted ledger', 'alpha')
    allowed = _add_k(db, 'Alpha ledger details', 'alpha')
    db.execute("UPDATE knowledge SET type='fact',branch='main'")
    if restricted == 'project':
        db.execute("UPDATE knowledge SET project='beta' WHERE id=?", (hidden,))
    elif restricted == 'branch':
        db.execute("UPDATE knowledge SET branch='private' WHERE id=?", (hidden,))
    elif restricted == 'tags':
        db.execute("UPDATE knowledge SET tags='[\"recovery\"]' WHERE id=?", (hidden,))
    else:
        db.execute("UPDATE knowledge SET type='solution' WHERE id=?", (hidden,))
    for kid in (seed, hidden, allowed):
        _link(db, kid, hub)
    scope = SearchScope(project='alpha', branch='main', kind='fact')
    assert ContextExpander(db).expand([seed], budget=1, scope=scope,
                                     excluded_tags=('recovery',)) == [allowed]


def test_expansion_without_project_keeps_cross_project_results(db, graph_store):
    from context_expander import ContextExpander
    from memory_core.retrieval import SearchScope

    hub = graph_store.add_node('concept', 'ledger')
    seed = _add_k(db, 'Alpha invoices', 'alpha')
    other = _add_k(db, 'Beta ledger', 'beta')
    for kid in (seed, other):
        _link(db, kid, hub)
    assert ContextExpander(db).expand([seed], scope=SearchScope()) == [other]
