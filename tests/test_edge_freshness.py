"""Tests for edge freshness in ContextExpander (Wave A — A2).

Fresh 1-hop graph edges contribute more to the expansion score than long-
dormant ones. This lets recently reinforced relationships rescue otherwise
buried nodes, which is the main lever for repairing recall when summaries
of those records age out.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from context_expander import ContextExpander, _edge_freshness


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _add_k(db, content: str, project: str = "demo") -> int:
    return db.execute(
        "INSERT INTO knowledge (content, project, status, created_at) "
        "VALUES (?, ?, 'active', ?)",
        (content, project, "2026-04-14T00:00:00Z"),
    ).lastrowid


def _link(db, kid: int, node_id: str, strength: float = 1.0):
    db.execute(
        "INSERT OR REPLACE INTO knowledge_nodes "
        "(knowledge_id, node_id, role, strength) VALUES (?, ?, 'mentions', ?)",
        (kid, node_id, strength),
    )
    db.commit()


# ──────────────────────────────────────────────
# Pure freshness curve
# ──────────────────────────────────────────────


def test_freshness_decays_with_age():
    fresh = _edge_freshness(_iso_days_ago(0), 60)
    middle = _edge_freshness(_iso_days_ago(60), 60)
    stale = _edge_freshness(_iso_days_ago(360), 60)
    assert fresh > middle > stale
    assert fresh > 0.99
    # 60d at hl=60 → 0.5
    assert 0.45 < middle < 0.55


def test_freshness_clamped_floor():
    """Even a 100-year-old edge stays above 0.05 so it can still nudge."""
    very_old = _edge_freshness(_iso_days_ago(36500), 60)
    assert very_old >= 0.05


def test_freshness_missing_ts_returns_default():
    assert _edge_freshness(None, 60) == 0.5
    assert _edge_freshness("", 60) == 0.5
    assert _edge_freshness("not-an-iso-date", 60) == 0.5


# ──────────────────────────────────────────────
# Integration with ContextExpander
# ──────────────────────────────────────────────


def _seed_two_neighbors_with_distinct_freshness(db, graph_store, days_fresh, days_stale):
    """Build: seed_node -- (fresh edge) --> n_fresh, seed_node -- (stale edge) --> n_stale.

    Knowledge records linked to n_fresh and n_stale. Returns (seed_kid, fresh_kid, stale_kid).
    """
    seed_node = graph_store.add_node("concept", "seed-topic")
    fresh_node = graph_store.add_node("concept", "fresh-topic")
    stale_node = graph_store.add_node("concept", "stale-topic")

    graph_store.add_edge(seed_node, fresh_node, "uses", weight=1.0)
    graph_store.add_edge(seed_node, stale_node, "uses", weight=1.0)

    # Override edge timestamps directly (graph_store sets created_at=now)
    db.execute(
        "UPDATE graph_edges SET created_at=?, last_reinforced_at=NULL "
        "WHERE source_id=? AND target_id=?",
        (_iso_days_ago(days_fresh), seed_node, fresh_node),
    )
    db.execute(
        "UPDATE graph_edges SET created_at=?, last_reinforced_at=NULL "
        "WHERE source_id=? AND target_id=?",
        (_iso_days_ago(days_stale), seed_node, stale_node),
    )
    db.commit()

    seed_kid = _add_k(db, "seed record")
    fresh_kid = _add_k(db, "record about fresh topic")
    stale_kid = _add_k(db, "record about stale topic")
    _link(db, seed_kid, seed_node)
    _link(db, fresh_kid, fresh_node)
    _link(db, stale_kid, stale_node)
    return seed_kid, fresh_kid, stale_kid


def test_fresh_edge_outranks_stale_edge_of_same_weight(db, graph_store):
    seed_kid, fresh_kid, stale_kid = _seed_two_neighbors_with_distinct_freshness(
        db, graph_store, days_fresh=0, days_stale=300
    )

    ex = ContextExpander(db, edge_half_life_days=60)
    result = ex.expand(seed_ids=[seed_kid], budget=10, depth=1)

    assert fresh_kid in result
    assert stale_kid in result
    # Fresh must come before stale because same weight, different freshness.
    assert result.index(fresh_kid) < result.index(stale_kid)


def test_reinforced_edge_beats_old_creation_date(db, graph_store):
    """An edge created long ago but recently reinforced should win over an
    edge created recently but never touched — last_reinforced_at takes
    precedence in our freshness calc."""
    seed_node = graph_store.add_node("concept", "seed-x")
    a_node = graph_store.add_node("concept", "reinforced-target")
    b_node = graph_store.add_node("concept", "untouched-target")
    graph_store.add_edge(seed_node, a_node, "uses", weight=1.0)
    graph_store.add_edge(seed_node, b_node, "uses", weight=1.0)

    # a: old creation, fresh reinforcement
    db.execute(
        "UPDATE graph_edges SET created_at=?, last_reinforced_at=? "
        "WHERE source_id=? AND target_id=?",
        (_iso_days_ago(300), _iso_days_ago(1), seed_node, a_node),
    )
    # b: fresh creation, no reinforcement
    db.execute(
        "UPDATE graph_edges SET created_at=?, last_reinforced_at=NULL "
        "WHERE source_id=? AND target_id=?",
        (_iso_days_ago(45), seed_node, b_node),
    )
    db.commit()

    seed_kid = _add_k(db, "seed x")
    a_kid = _add_k(db, "reinforced")
    b_kid = _add_k(db, "untouched")
    _link(db, seed_kid, seed_node)
    _link(db, a_kid, a_node)
    _link(db, b_kid, b_node)

    ex = ContextExpander(db, edge_half_life_days=60)
    result = ex.expand(seed_ids=[seed_kid], budget=10, depth=1)
    assert result.index(a_kid) < result.index(b_kid)


def test_env_override_for_edge_half_life(monkeypatch, db, graph_store):
    """``MEMORY_EDGE_HALF_LIFE_DAYS`` env override is respected when the
    expander is constructed without an explicit half-life."""
    monkeypatch.setenv("MEMORY_EDGE_HALF_LIFE_DAYS", "10")
    ex = ContextExpander(db)
    # Internal state should reflect env. Not part of public API but stable
    # enough to assert in the test suite.
    assert ex._edge_half_life == 10
