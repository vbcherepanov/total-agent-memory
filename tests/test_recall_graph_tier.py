"""The recall graph tier walks the knowledge graph, not only the sparse `relations` table."""

from __future__ import annotations

import pytest

import config
import server


def _insert(store, identity: int, content: str, project: str = "p") -> None:
    store.db.execute(
        "INSERT INTO knowledge(id,content,session_id,project,type,status,created_at,last_confirmed) "
        "VALUES (?,?,'s',?,'fact','active','2026-01-01','2026-01-01')",
        (identity, content, project),
    )


def _node(store, node_id: str, name: str, node_type: str) -> None:
    store.db.execute("INSERT INTO graph_nodes(id,type,name) VALUES (?,?,?)", (node_id, node_type, name))


def _link(store, knowledge_id: int, node_id: str, strength: float = 1.0) -> None:
    store.db.execute("INSERT INTO knowledge_nodes(knowledge_id,node_id,role,strength) VALUES (?,?,'mentions',?)",
                     (knowledge_id, node_id, strength))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    instance = server.Store()
    instance._embed_mode = "none"
    monkeypatch.setattr(instance, "bump_recall", lambda ids: None)
    _insert(instance, 1, "pgbouncer pool size raised to 200 for the billing service")
    _insert(instance, 2, "billing service outage root cause: connection exhaustion")
    _insert(instance, 3, "frontend switched to nuxt 4")
    _insert(instance, 4, "unrelated note about lunch")
    _node(instance, "n-billing", "billing", "concept")
    _node(instance, "n-project", "p", "project")
    _node(instance, "n-hub", "api", "concept")
    _link(instance, 1, "n-billing")
    _link(instance, 2, "n-billing", 0.5)
    for kid in (1, 2, 3, 4):
        _link(instance, kid, "n-project")
    instance.db.commit()
    yield instance
    instance.db.close()


def ids(result):
    return [hit["id"] for group in result["results"].values() for hit in group]


def test_records_sharing_an_entity_node_join_via_the_graph_tier(store):
    result = server.Recall(store).search("pgbouncer pool size", project="p", limit=5, _explain=True)
    assert 1 in ids(result)
    assert 2 in ids(result), "record 2 shares the 'billing' node with the seed"
    assert "graph" in result["_explain"]["tiers_used"]
    assert any(item["id"] == 2 for item in result["_explain"]["graph"])
    assert 3 not in ids(result) and 4 not in ids(result), "the project node is a hub and must not connect everything"


def test_graph_tier_scores_are_below_the_seed(store):
    result = server.Recall(store).search("pgbouncer pool size", project="p", limit=5, _explain=True)
    ordered = ids(result)
    assert ordered.index(1) < ordered.index(2)


def test_weight_zero_switches_the_graph_tier_off(store):
    result = server.Recall(store).search("pgbouncer pool size", project="p", limit=5, _explain=True,
                                         tier_weights={"graph": 0})
    assert 2 not in ids(result)
    assert "graph" not in result["_explain"]["tiers_used"]


def test_nodes_above_the_hub_degree_are_ignored(store, monkeypatch):
    for kid in (1, 2, 3, 4):
        _link(store, kid, "n-hub")
    store.db.commit()
    monkeypatch.setenv("MEMORY_GRAPH_HUB_DEGREE", "3")
    result = server.Recall(store).search("pgbouncer pool size", project="p", limit=5, _explain=True)
    assert 2 in ids(result), "'billing' (degree 2) still counts"
    assert 3 not in ids(result) and 4 not in ids(result), "'api' (degree 4 > 3) is a hub"


def test_hub_degree_env_parsing(monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_HUB_DEGREE", "50")
    assert config.get_graph_hub_degree() == 50
    monkeypatch.setenv("MEMORY_GRAPH_HUB_DEGREE", "0")
    assert config.get_graph_hub_degree() == config.DEFAULT_GRAPH_HUB_DEGREE
    monkeypatch.setenv("MEMORY_GRAPH_HUB_DEGREE", "many")
    assert config.get_graph_hub_degree() == config.DEFAULT_GRAPH_HUB_DEGREE


def test_explicit_relations_still_count(store):
    store.db.execute("INSERT INTO relations(from_id,to_id,type,created_at) VALUES (1,3,'related','2026-01-01')")
    store.db.commit()
    result = server.Recall(store).search("pgbouncer pool size", project="p", limit=5, _explain=True)
    assert 3 in ids(result)
