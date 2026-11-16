import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.pg_store_support import store_backend  # noqa: F401

T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T01:00:00Z"
T2 = "2026-01-01T02:00:00Z"
TD = "2026-01-01T02:30:00Z"
T3 = "2026-01-01T03:00:00Z"


@pytest.mark.parametrize("value", [
    "2026-01-01T04:00:00+03:00",
    datetime(2026, 1, 1, 4, tzinfo=timezone(timedelta(hours=3))),
    datetime(2026, 1, 1, 1),
])
def test_snapshot_anchor_normalizes_offsets(value):
    import server
    assert server._parse_as_of(value) == datetime(2026, 1, 1, 1)


@pytest.mark.parametrize("overrides", [
    {"query": " "}, {"query": None}, {"as_of": None}, {"as_of": "invalid"},
    {"limit": 0}, {"limit": True}, {"limit": "1"}, {"project": []},
])
def test_snapshot_query_rejects_invalid_arguments(overrides):
    from memory_core.snapshot import SnapshotQuery
    with pytest.raises(ValueError):
        SnapshotQuery.from_arguments({"query": "cobalt", "as_of": T1, **overrides})


@pytest.fixture
def snapshot_server(store_backend, monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    instance = server.Store(database=store_backend, background_queues=False)
    instance._embed_mode = "none"
    monkeypatch.setattr(server, "store", instance)
    monkeypatch.setattr(server, "recall", server.Recall(instance))
    monkeypatch.setattr(server, "SID", "snapshot-test")
    monkeypatch.setattr(server, "_v5_modules", {})
    clock = [T1]
    monkeypatch.setattr(server, "utc_now", lambda: clock[0])

    def save(content, supersedes=None, project="snapshot"):
        return instance.save_knowledge("snapshot-test", content, "fact", project, [], "",
                                       skip_dedup=True, skip_quality=True, supersedes=supersedes)[0]

    first = save("Routing dispatch uses cobalt")
    deleted = save("Routing dispatch obsolete endpoint")
    clock[0] = T2
    second = save("Routing dispatch uses amber", supersedes=first)
    clock[0] = TD
    instance.delete_knowledge(deleted)
    clock[0] = T3
    third = save("Routing dispatch uses jade", supersedes=second)
    save("Routing dispatch belongs to another project", project="elsewhere")
    yield server, save, (first, second, third), deleted
    instance.db.close()


def _ids(result):
    return {hit["id"] for group in result["results"].values() for hit in group}


def test_snapshot_state_history_tracks_exact_boundaries(snapshot_server):
    from memory_core.snapshot import Snapshot
    server, _, (first, second, third), deleted = snapshot_server
    ids = (first, second, third, deleted)
    for timestamp, expected in ((T0, set()), (T1, {first, deleted}),
                                (T2, {second, deleted}), (TD, {second}), (T3, {third})):
        snapshot = Snapshot.at(timestamp)
        visible = {kid for kid in ids if snapshot.allows(server.store.db, kid)}
        assert visible == expected, timestamp
    assert Snapshot.at("2026-01-01T04:00:00+03:00") == Snapshot.at(T1)


def test_hard_erasure_removes_snapshot_history(snapshot_server):
    from memory_core.snapshot import Snapshot
    server, _, (first, _, _), _ = snapshot_server
    server.store.db.execute("DELETE FROM knowledge WHERE id=?", (first,))
    server.store.db.commit()
    assert not Snapshot.at(T1).allows(server.store.db, first)
    assert server.store.db.execute("SELECT count(*) FROM knowledge_state_events WHERE knowledge_id=?",
                                   (first,)).fetchone()[0] == 0


def test_state_history_rolls_back_and_tracks_reactivation(snapshot_server):
    from memory_core.snapshot import Snapshot
    server, _, (_, _, third), _ = snapshot_server
    db = server.store.db
    db.execute("UPDATE knowledge SET status='archived', state_changed_at=? WHERE id=?",
               ("2026-01-01T04:00:00Z", third))
    db.rollback()
    assert Snapshot.at("2026-01-01T04:30:00Z").allows(db, third)
    db.execute("UPDATE knowledge SET status='archived', state_changed_at=? WHERE id=?",
               ("2026-01-01T04:00:00Z", third))
    db.execute("UPDATE knowledge SET status='active', state_changed_at=? WHERE id=?",
               ("2026-01-01T05:00:00Z", third))
    db.commit()
    assert not Snapshot.at("2026-01-01T04:30:00Z").allows(db, third)
    assert Snapshot.at("2026-01-01T05:00:00Z").allows(db, third)


def test_search_scope_selects_historical_records_before_limit(snapshot_server):
    from memory_core.retrieval import SearchScope
    server, _, (first, _, _), deleted = snapshot_server
    scope = SearchScope(project="snapshot", kind="fact", as_of=T1)
    predicates, params = scope.sql()
    rows = server.store.db.execute(f"SELECT k.* FROM knowledge k WHERE {' AND '.join(predicates)}", params).fetchall()
    assert {row["id"] for row in rows} == {first, deleted}
    assert all(scope.allows(dict(row), server.store.db) for row in rows)


def test_snapshot_state_history_preserves_fractional_and_offset_timestamps(snapshot_server, monkeypatch):
    from memory_core.snapshot import Snapshot
    server, save, _, _ = snapshot_server
    for timestamp in ("2026-01-02T00:00:00.0005Z", "2026-01-02T23:59:59.9996Z",
                      "2026-01-03T02:00:00+02:00"):
        monkeypatch.setattr(server, "utc_now", lambda: timestamp)
        kid = save(f"Fractional boundary {timestamp}")
        assert Snapshot.at(timestamp).allows(server.store.db, kid)
        assert not Snapshot.at(T0).allows(server.store.db, kid)


def test_snapshot_semantic_reconstructs_only_selected_versions_and_reuses_cache(snapshot_server, monkeypatch):
    from memory_core.retrieval import SearchScope
    from memory_core.snapshot_batch import SnapshotBatch
    server, _, (first, _, _), deleted = snapshot_server
    store = server.store
    store._embed_mode = "fastembed"
    original = SnapshotBatch.execute
    encoded = []

    def encode(batch):
        encoded.extend(text.rstrip() for text, _space in batch.records)
        return original(batch)

    monkeypatch.setattr(SnapshotBatch, "execute", encode)
    scope = SearchScope(project="snapshot", as_of=T1)
    hits = store._search_spaces("Routing dispatch uses cobalt", project="snapshot", scope=scope, limit=2)
    assert hits[0][0] == first
    assert {kid for kid, _ in hits} == {first, deleted}
    assert len(encoded) == 2
    assert not store.db.in_transaction
    assert store._search_spaces("Routing dispatch uses cobalt", project="snapshot", scope=scope, limit=2) == hits
    assert len(encoded) == 2
    store.db.execute("DELETE FROM knowledge WHERE id=?", (first,))
    store.db.commit()
    assert store.db.execute("SELECT COUNT(*) FROM snapshot_embedding_cache WHERE knowledge_id=?", (first,)).fetchone()[0] == 0
    assert [kid for kid, _ in store._search_spaces("Routing dispatch uses cobalt", project="snapshot", scope=scope, limit=2)] == [deleted]


def test_snapshot_boundaries_and_current_search(snapshot_server):
    server, _, (first, second, third), deleted = snapshot_server
    for timestamp, expected in ((T0, set()), (T1, {first, deleted}),
                                (T2, {second, deleted}), (TD, {second}), (T3, {third})):
        result = server.recall.search("Routing dispatch", project="snapshot", as_of=timestamp, limit=10)
        assert _ids(result) == expected, timestamp
    assert _ids(server.recall.search("Routing dispatch", project="snapshot", limit=10)) == {third}


def test_snapshot_selected_before_candidate_limits(snapshot_server):
    server, save, (first, _, _), _ = snapshot_server
    for index in range(20):
        save(f"Routing dispatch future cobalt expansion {index}")
    result = server.recall.search("cobalt", project="snapshot", as_of=T1, limit=1, _explain=True)
    assert _ids(result) == {first}, json.dumps(result, ensure_ascii=False)


def test_temporal_query_snapshot_uses_same_version_selection(snapshot_server):
    server, _, (first, _, _), deleted = snapshot_server
    result, is_error = asyncio.run(server._call_tool_impl("memory_temporal_query", {
        "op": "snapshot", "as_of": T1, "query": "Routing dispatch", "project": "snapshot", "limit": 10,
    }))
    assert not is_error
    assert _ids(json.loads(result[0].text)) == {first, deleted}


def test_snapshot_auxiliary_queries_select_versions_before_limits(snapshot_server):
    from memory_core.retrieval import SearchScope
    from memory_core.retrieval_tiers import (
        compound_part_winners, directive_candidates, lexical_candidates, multi_query_candidates,
    )
    server, save, (first, _, _), _ = snapshot_server
    for index in range(20):
        save(f"Cobalt future configuration {index}")
    server.store.db.execute("UPDATE knowledge SET type='convention'")
    server.store.db.commit()
    scope = SearchScope(project="snapshot", as_of=T1)
    for project in (None, "snapshot"):
        lexical = lexical_candidates(server.store, "cobalt", project=project, ktype="all", branch=None,
                                      limit=1, scope=SearchScope(project=project, as_of=T1))
        assert [kid for kid, _ in lexical] == [first]
    common = dict(project="snapshot", branch=None, can_embed=False, scope=scope)
    assert [kid for kid, _ in directive_candidates(server.store, "cobalt", limit=1, **common)] == [first]
    multi = dict(fuse=server.recall._rrf_fuse, ktype="all", spaces=None, **common)
    assert [kid for kid, _ in multi_query_candidates(server.store, ["cobalt"], limit=1, **multi)] == [first]
    assert compound_part_winners(server.store, ["cobalt"], **multi) == [first]
    result = server.recall.search("How should I configure cobalt?", project="snapshot", as_of=T1,
                                  limit=1, _explain=True)
    assert {hit["id"] for hit in result["_explain"]["directives"]} == {first}


def test_snapshot_graph_counts_only_visible_records_before_hub_cutoff(snapshot_server, monkeypatch):
    from memory_core.retrieval import SearchScope
    from memory_core.retrieval_tiers import query_entity_candidates, shared_entity_neighbours
    server, save, (first, _, _), deleted = snapshot_server
    monkeypatch.setenv("MEMORY_GRAPH_HUB_DEGREE", "2")
    future = [save(f"Unrelated future graph record {index}") for index in range(5)]
    db = server.store.db
    db.execute("INSERT INTO graph_nodes(id,type,name) VALUES ('snapshot-node','concept','cobalt')")
    for kid in [first, deleted, *future]:
        db.execute("INSERT INTO knowledge_nodes(knowledge_id,node_id,strength) VALUES (?,'snapshot-node',1)", (kid,))
    db.commit()
    scope = SearchScope(project="snapshot", as_of=T1)
    neighbours = shared_entity_neighbours(server.store, [first], {first: 1.0}, limit=1, scope=scope)
    assert [kid for kid, _ in neighbours] == [deleted]
    hits = query_entity_candidates(server.store, "cobalt", project="snapshot", ktype="all", branch=None,
                                   limit=2, scope=scope)
    assert {kid for kid, _ in hits} == {first, deleted}


def test_kg_at_uses_half_open_intervals(snapshot_server):
    from temporal_kg import TemporalKG
    server, _, _, _ = snapshot_server
    kg = TemporalKG(server.store.db)
    for timestamp, value in ((T1, "cobalt"), (T2, "amber"), (T3, "jade")):
        kg.add_fact("routing", "uses", value, project="snapshot", valid_from=timestamp)
    for timestamp, value in ((T0, None), (T1, "cobalt"), (T2, "amber"), (T3, "jade")):
        rows = kg.query_at(timestamp, subject="routing", predicate="uses", project="snapshot")
        assert [row["object"] for row in rows] == ([] if value is None else [value])


def test_snapshot_representations_do_not_reuse_current_cache(snapshot_server):
    from memory_core.embedding_identity import EmbedIdentity
    from memory_core.retrieval import SearchScope
    from multi_repr_search import RepresentationCache, search_with_winners
    from multi_repr_store import MultiReprStore
    server, _, (first, second, third), _ = snapshot_server
    db = server.store.db
    identity = EmbedIdentity("snapshot-test-vector", "declared")
    vector = [1.0, 0.0, 0.0]
    for kid in (first, second, third):
        MultiReprStore(db).upsert(kid, "summary", "routing summary", vector, identity.model, identity=identity)
    cache = RepresentationCache(db)
    args = dict(project="snapshot", top_n=1, n_candidates=1, query_identity=identity, cache=cache)
    current, _ = search_with_winners(db, vector, **args)
    assert [kid for kid, _ in current] == [third]
    assert cache.cache_bytes > 0
    for timestamp, expected in ((T0, []), (T1, [first]), (T2, [second]), (T3, [third])):
        hits, winners = search_with_winners(db, vector, scope=SearchScope(project="snapshot", as_of=timestamp), **args)
        assert [kid for kid, _ in hits] == expected
        assert winners == {kid: "summary" for kid in expected}
    current, _ = search_with_winners(db, vector, **args)
    assert [kid for kid, _ in current] == [third]


def test_snapshot_multi_hop_filters_each_step(snapshot_server, monkeypatch):
    from memory_core.retrieval import SearchScope
    from reranker import multi_hop_expand
    server, save, (first, second, _), deleted = snapshot_server
    monkeypatch.setattr(server, "utc_now", lambda: T1)
    foreign = save("Foreign graph bridge", project="elsewhere")
    db = server.store.db
    for source, target in ((first, deleted), (deleted, second), (first, foreign)):
        db.execute("INSERT INTO relations(from_id,to_id,type,created_at) VALUES (?,?,'related',?)",
                    (source, target, T1))
    db.commit()
    row = dict(db.execute("SELECT * FROM knowledge WHERE id=?", (first,)).fetchone())
    results = {first: {"r": row, "score": 1.0, "via": ["fts"]}}
    scope = SearchScope(project="snapshot", as_of=T1)
    expanded = multi_hop_expand(server.store, [first], results, depth=2, scope=scope)
    assert set(expanded) == {first, deleted}
    assert expanded[deleted]["score"] == pytest.approx(0.4)


def test_snapshot_episodes_filter_constituents_before_channel_limits(snapshot_server):
    import struct
    from memory_core.episodes.retriever import _bm25_fts, _bm25_like, _cosine_search, retrieve_episodes
    from memory_core.retrieval import SearchScope
    server, _, (first, second, _), _ = snapshot_server
    db = server.store.db

    def episode(index, facts, end=T1):
        eid = db.execute(
            "INSERT INTO episodes_v11(project,session_id,started_at,ended_at,summary,embedding_blob) "
            "VALUES ('snapshot',?,?,?,?,?)",
            (f"episode-{index}", T1, end, "Cobalt routing", struct.pack("2f", 1.0, 0.0)),
        ).lastrowid
        if not server.store.is_postgres:
            db.execute("INSERT INTO episodes_v11_fts(rowid,summary,participants,outcome) VALUES (?,?,'','')",
                        (eid, "Cobalt routing"))
        for kid in facts:
            db.execute("INSERT INTO episode_facts(episode_id,knowledge_id) VALUES (?,?)", (eid, kid))
        return eid

    for index in range(30):
        episode(index, [second])
    episode("mixed", [first, second])
    episode("unlinked", [])
    episode("future-window", [first], end=T3)
    expected = episode("historical", [first])
    db.commit()
    scope = SearchScope(project="snapshot", as_of=T1)
    hits = retrieve_episodes(db, "cobalt", "snapshot", k=1, embed_fn=lambda _: [1.0, 0.0], scope=scope)
    assert [hit.episode_id for hit in hits] == [expected]
    assert hits[0].fact_ids == (first,)
    assert [eid for eid, _ in _bm25_fts(db, "cobalt", "snapshot", 1, scope=scope)] == [expected]
    assert [eid for eid, _ in _bm25_like(db, "cobalt", "snapshot", 1, scope=scope)] == [expected]
    assert [eid for eid, _ in _cosine_search(db, "cobalt", "snapshot", 1,
                                           lambda _: [1.0, 0.0], scope=scope)] == [expected]
    result = server.recall.search(
        "cobalt", project="snapshot", as_of=T1, limit=1,
        tier_weights={name: 0 for name in ("fts", "semantic", "fuzzy", "graph", "multi_repr", "hyde",
                                          "atomic_facts", "directives", "query_entity", "multi_query")},
    )
    assert _ids(result) == {first}
    assert result["results"]["fact"][0]["via"] == ["episode"]


def test_snapshot_space_filter_prepares_cold_cache_before_lexical_limits(snapshot_server):
    from memory_core.retrieval import SearchScope
    server, _, (first, _, _), _ = snapshot_server
    db = server.store.db
    for semantic_weight in (1.2, 0):
        db.execute("DELETE FROM snapshot_embedding_cache")
        db.commit()
        result = server.recall.search("cobalt", project="snapshot", as_of=T1, embedding_space="text",
                                      limit=1, _explain=True, tier_weights={"semantic": semantic_weight})
        assert _ids(result) == {first}
        assert {hit["id"] for hit in result["_explain"]["fts"]} == {first}
        row = dict(db.execute("SELECT * FROM knowledge WHERE id=?", (first,)).fetchone())
        assert SearchScope(project="snapshot", spaces="text", as_of=T1).allows(row, db)
        assert not SearchScope(project="snapshot", spaces="code", as_of=T1).allows(row, db)


def test_snapshot_hyde_searches_historical_vectors(snapshot_server, monkeypatch):
    server, _, (first, _, _), deleted = snapshot_server
    monkeypatch.setattr(server.recall, "_should_use_advanced_rag", lambda: True)
    monkeypatch.setattr(server, "analyze_query", lambda _: {"expand": True})

    def hypothetical_vector(query, project, embed_fn):
        return embed_fn(["Routing dispatch uses cobalt"])[0]

    monkeypatch.setattr(server, "hyde_expand", hypothetical_vector)
    result = server.recall.search(
        "routing configuration", project="snapshot", as_of=T1, limit=1, _explain=True,
        tier_weights={name: 0 for name in ("fts", "semantic", "fuzzy", "graph", "multi_repr", "episode",
                                          "atomic_facts", "directives", "query_entity", "multi_query")},
    )
    assert _ids(result) == {first}
    assert result["results"]["fact"][0]["via"] == ["hyde"]
    assert {hit["id"] for hit in result["_explain"]["hyde"]} <= {first, deleted}


def test_snapshot_space_preparation_preserves_outer_transaction(snapshot_server):
    server, _, (first, _, _), deleted = snapshot_server
    db = server.store.db
    db.execute("UPDATE knowledge SET context='uncommitted context' WHERE id=?", (first,))
    try:
        result = server.recall.search("Routing dispatch", project="snapshot", as_of=T1,
                                      embedding_space="text", record_usage=False)
        assert _ids(result) == {first, deleted}
        assert db.in_transaction
        assert db.execute("SELECT count(*) FROM snapshot_embedding_cache").fetchone()[0] == 2
    finally:
        db.rollback()
    assert db.execute("SELECT context FROM knowledge WHERE id=?", (first,)).fetchone()[0] == ""
    assert db.execute("SELECT count(*) FROM snapshot_embedding_cache").fetchone()[0] == 0


def test_snapshot_cache_rebuilds_for_recipe_and_content_changes(snapshot_server, monkeypatch):
    from memory_core.retrieval import SearchScope
    from memory_core.snapshot_batch import SnapshotBatch
    server, _, (first, _, _), _ = snapshot_server
    store = server.store
    original = SnapshotBatch.execute
    recipe = store._recipe
    encoded = []

    def encode(batch):
        encoded.extend(text.rstrip() for text, _space in batch.records)
        return original(batch)

    monkeypatch.setattr(SnapshotBatch, "execute", encode)
    scope = SearchScope(project="snapshot", as_of=T1)
    store._search_spaces("routing", scope=scope)
    assert len(encoded) == 2
    monkeypatch.setattr(store, "_recipe", lambda provider: (recipe(provider)[0], recipe(provider)[1] + "-updated"))
    store._search_spaces("routing", scope=scope)
    assert len(encoded) == 4
    store.db.execute("UPDATE knowledge SET content='Routing dispatch uses violet' WHERE id=?", (first,))
    store.db.commit()
    store._search_spaces("routing", scope=scope)
    assert len(encoded) == 5
    assert encoded[-1] == "Routing dispatch uses violet"
    store._search_spaces("routing", scope=scope)
    assert len(encoded) == 5


def test_snapshot_context_expansion_hydrates_retired_records(snapshot_server):
    from context_expander import ContextExpander
    from memory_core.retrieval import SearchScope
    server, _, (first, _, third), deleted = snapshot_server
    db = server.store.db
    db.execute("INSERT INTO graph_nodes(id,type,name) VALUES ('snapshot-context','concept','routing')")
    for kid in (first, deleted, third):
        db.execute("INSERT INTO knowledge_nodes(knowledge_id,node_id,strength) VALUES (?,'snapshot-context',1)", (kid,))
    db.commit()
    assert ContextExpander(db).expand([first], budget=1, scope=SearchScope(project="snapshot", as_of=T1)) == [deleted]
    result, is_error = asyncio.run(server._call_tool_impl("memory_recall", {
        "query": "cobalt", "project": "snapshot", "as_of": T1, "limit": 1,
        "expand_context": True, "expand_budget": 1,
    }))
    assert not is_error
    payload = json.loads(result[0].text)
    assert _ids(payload) == {first}
    assert [row["id"] for row in payload["expansion"]] == [deleted]


@pytest.mark.parametrize("mode", ["index", "context", "timeline"])
def test_snapshot_modes_keep_historical_neighbors(snapshot_server, monkeypatch, mode):
    server, save, (first, _, _), deleted = snapshot_server
    monkeypatch.setattr(server, "utc_now", lambda: "2026-01-01T00:30:00Z")
    neighbor = save("Historical routing note")
    monkeypatch.setattr(server, "utc_now", lambda: TD)
    server.store.delete_knowledge(neighbor)
    monkeypatch.setattr(server, "utc_now", lambda: "2026-01-01T00:59:00Z")
    for index in range(20):
        save(f"Foreign timeline noise {index}", project="elsewhere")
    result, is_error = asyncio.run(server._call_tool_impl("memory_recall", {
        "query": "cobalt", "project": "snapshot", "as_of": T1, "limit": 1,
        "mode": mode, "neighbors": 1,
    }))
    assert not is_error
    payload = json.loads(result[0].text)
    ids = {record["id"] for record in payload["results"]}
    assert first in ids
    if mode == "index":
        assert ids == {first}
    else:
        assert neighbor in ids
        assert ids <= {first, deleted, neighbor}


@pytest.mark.parametrize("followup", [False, True])
def test_snapshot_evidence_preserves_scope_in_followup(snapshot_server, monkeypatch, followup):
    server, _, (first, _, _), deleted = snapshot_server
    original = server.recall.search
    instants = []

    def search(*args, **kwargs):
        instants.append(kwargs.get("as_of"))
        return original(*args, **kwargs)

    monkeypatch.setattr(server.recall, "search", search)
    result, is_error = asyncio.run(server._call_tool_impl("memory_recall", {
        "query": "cobalt", "project": "snapshot", "as_of": T1, "limit": 2,
        "mode": "evidence", "evidence_followup": followup,
        "missing_relation": {"subject": "cobalt", "relation": "dispatch"},
    }))
    assert not is_error
    payload = json.loads(result[0].text)
    assert {record["id"] for record in payload["results"]} == {first, deleted}
    assert instants == [T1] * (2 if followup else 1)


@pytest.mark.parametrize("channel", ["expand_top_k", "expand_via_associations", "expand"])
def test_snapshot_graph_expansion_filters_before_budget(snapshot_server, channel):
    import graph_expander
    from memory_core.retrieval import SearchScope
    server, _, (first, _, third), deleted = snapshot_server
    db = server.store.db
    foreign = db.execute("SELECT id FROM knowledge WHERE project='elsewhere'").fetchone()[0]
    for node in ("scope-seed", "scope-neighbor"):
        db.execute("INSERT INTO graph_nodes(id,type,name) VALUES (?,'concept',?)", (node, node))
    db.execute("INSERT INTO knowledge_nodes(knowledge_id,node_id,strength) VALUES (?,'scope-seed',1)", (first,))
    db.execute("INSERT INTO graph_edges(id,source_id,target_id,relation_type,weight) VALUES ('scope-edge','scope-seed','scope-neighbor','related',1)")
    for kid, strength in ((foreign, 1.0), (third, 0.9), (deleted, 0.5)):
        db.execute("INSERT INTO knowledge_nodes(knowledge_id,node_id,strength) VALUES (?,'scope-neighbor',?)", (kid, strength))
        db.execute("INSERT INTO relations(from_id,to_id,type) VALUES (?,?,'related')", (first, kid))
    db.commit()
    scope = SearchScope(project="snapshot", kind="fact", as_of=T1)
    expand = getattr(graph_expander, channel)
    assert [kid for kid, _ in expand(db, [first], budget=1, scope=scope)] == [deleted]
    assert [row["id"] for row in graph_expander.fetch_records(db, [foreign, third, deleted], scope=scope)] == [deleted]
    current = SearchScope(project="snapshot", kind="fact")
    assert [kid for kid, _ in expand(db, [first], budget=1, scope=current)] == [third]


@pytest.mark.parametrize("entities", [["Cobalt"], ["Cobalt", "Dispatch"]])
def test_snapshot_router_filters_events_before_limit(snapshot_server, monkeypatch, entities):
    import query_router
    from memory_core.retrieval import SearchScope
    server, _, (first, _, third), _ = snapshot_server
    db = server.store.db
    foreign = db.execute("SELECT id FROM knowledge WHERE project='elsewhere'").fetchone()[0]
    for name in entities:
        db.execute("INSERT INTO graph_nodes(id,type,name) VALUES (?,'concept',?)", (name, name))
    for kid, timestamp in ((first, T1), (third, T3), (foreign, T3)):
        event = f"router-event-{kid}"
        properties = json.dumps({"knowledge_id": kid, "project": "snapshot", "timestamp": timestamp})
        db.execute("INSERT INTO graph_nodes(id,type,name,properties,last_seen_at) VALUES (?,'event',?,?,?)", (event, event, properties, timestamp))
        for name in entities:
            db.execute("INSERT INTO graph_edges(id,source_id,target_id,relation_type,weight) VALUES (?,?,?,'mentioned_in',1)", (event + name, name, event))
    db.commit()
    scope = SearchScope(project="snapshot", as_of=T1)
    hits = query_router.graph_search(db, entities=entities, project="snapshot", limit=1, scope=scope)
    assert [row["id"] for row in hits] == [first]
    current = query_router.graph_search(db, entities=entities, project="snapshot", limit=1, scope=SearchScope(project="snapshot"))
    assert [row["id"] for row in current] == [third]
    observed = []
    original_router = query_router.graph_search
    original_expand = server._graph_expand

    def route(*args, **kwargs):
        observed.append(("router", kwargs.get("scope")))
        return original_router(*args, **kwargs)

    def expand(*args, **kwargs):
        observed.append(("expand", kwargs.get("scope")))
        return original_expand(*args, **kwargs)

    monkeypatch.setattr(query_router, "graph_search", route)
    monkeypatch.setattr(server, "_graph_expand", expand)
    monkeypatch.setenv("MEMORY_SMART_ROUTER_FORCE", "relational")
    monkeypatch.setenv("MEMORY_RECALL_TIER_WEIGHTS", "query_entity=0")
    monkeypatch.setenv("MEMORY_GRAPH_EXPAND", "1")
    result = server.recall.search("Cobalt", project="snapshot", as_of=T1, limit=2)
    assert first in _ids(result)
    assert {name for name, _ in observed} == {"router", "expand"}
    from memory_core.snapshot import Snapshot
    for _, selected_scope in observed:
        assert selected_scope.project == "snapshot"
        assert Snapshot.at(selected_scope.as_of) == Snapshot.at(T1)


@pytest.mark.parametrize("channel", ["lexical", "activation", "mcp"])
def test_snapshot_cognitive_solutions_keep_historical_scope(snapshot_server, monkeypatch, channel):
    from cognitive.engine import CognitiveEngine
    from memory_core.retrieval import SearchScope
    server, save, (first, _, third), deleted = snapshot_server
    db = server.store.db
    monkeypatch.setattr(server, "utc_now", lambda: T1)
    noise = [save(f"Cobalt routing foreign solution {index}", project="elsewhere") for index in range(12)]
    db.execute("UPDATE knowledge SET type='solution'")
    db.execute("INSERT INTO graph_nodes(id,type,name) VALUES ('cobalt-scope','concept','cobalt')")
    db.execute("INSERT INTO graph_nodes(id,type,name) VALUES ('solution-scope','concept','historical remedies')")
    db.execute("INSERT INTO graph_edges(id,source_id,target_id,relation_type,weight) VALUES ('cognitive-scope-edge','cobalt-scope','solution-scope','related',1)")
    for kid in [*noise, third, deleted, first]:
        db.execute("INSERT INTO knowledge_nodes(knowledge_id,node_id,strength) VALUES (?,'solution-scope',?)", (kid, 2 if kid in noise else 1))
    db.commit()
    engine = CognitiveEngine(db)
    scope = SearchScope(project="snapshot", as_of=T1)
    if channel == "mcp":
        server._v5_modules["cognitive"] = engine
        result, is_error = asyncio.run(server._call_tool_impl("memory_recall", {
            "query": "cobalt", "project": "snapshot", "as_of": T1, "limit": 1,
        }))
        assert not is_error
        payload = json.loads(result[0].text)
        assert _ids(payload) == {first}
        assert {row["id"] for row in payload["cognitive"]["additional_solutions"]} == {deleted}
        assert payload["cognitive"]["as_of"] == "2026-01-01T01:00:00.000000Z"
        assert "activated_concepts" in payload["cognitive"]["current_metadata"]
        assert "additional_solutions" not in payload["cognitive"]["current_metadata"]
        result, is_error = asyncio.run(server._call_tool_impl("memory_recall", {
            "query": "cobalt", "project": "snapshot", "limit": 1,
        }))
        assert not is_error
        current = json.loads(result[0].text)["cognitive"]
        assert "as_of" not in current
        assert "current_metadata" not in current
    else:
        def search(selected_scope):
            if channel == "lexical":
                return engine._solutions_matching(["routing"], "snapshot", scope=selected_scope)
            return engine._find_solutions(["unmatchedconcept"], "snapshot", {"solution-scope": 1}, scope=selected_scope)
        assert {row["id"] for row in search(scope)} == {first, deleted}
        assert {row["id"] for row in search(SearchScope(project="snapshot"))} == {third}
        assert search(SearchScope(project="snapshot", kind="fact", as_of=T1)) == []


def test_cognitive_skill_project_scope_on_both_backends(snapshot_server):
    from cognitive.engine import CognitiveEngine
    server, _, _, _ = snapshot_server
    db = server.store.db
    for identity, projects in (("local", '["snapshot"]'), ("global", '[]'), ("unset", None), ("foreign", '["snapshot-other"]')):
        db.execute("INSERT INTO skills(id,name,trigger_pattern,steps,projects,status) VALUES (?,?,'cobalt','[]',?,'active')", (identity, identity, projects))
    db.commit()
    engine = CognitiveEngine(db)
    assert {row["id"] for row in engine._find_applicable_skills(["cobalt"], "snapshot")} == {"local", "global", "unset"}
    assert {row["id"] for row in engine._find_applicable_skills(["cobalt"], None)} == {"local", "global", "unset", "foreign"}


def test_snapshot_cognitive_graph_rules_follow_linked_versions(snapshot_server):
    from cognitive.engine import CognitiveEngine
    from memory_core.retrieval import SearchScope
    server, _, (first, _, third), _ = snapshot_server
    db = server.store.db
    foreign = db.execute("SELECT id FROM knowledge WHERE project='elsewhere'").fetchone()[0]
    db.execute("INSERT INTO graph_nodes(id,type,name) VALUES ('rule-seed','concept','routing')")
    db.execute("INSERT INTO graph_nodes(id,type,name) VALUES ('rule-seed-two','concept','dispatch')")
    candidates = [("old-rule", first, 0.1), ("new-rule", third, 0.9)]
    candidates.extend((f"foreign-rule-{index}", foreign, 1.0) for index in range(6))
    for identity, kid, importance in candidates:
        db.execute("INSERT INTO graph_nodes(id,type,name,content,importance) VALUES (?,'rule',?,?,?)", (identity, identity, identity, importance))
        db.execute("INSERT INTO graph_edges(id,source_id,target_id,relation_type) VALUES (?,'rule-seed',?,'related')", (identity + '-edge', identity))
        db.execute("INSERT INTO knowledge_nodes(knowledge_id,node_id,strength) VALUES (?,?,1)", (kid, identity))
    db.execute("INSERT INTO graph_edges(id,source_id,target_id,relation_type) VALUES ('old-rule-second-edge','rule-seed-two','old-rule','related')")
    db.commit()
    engine = CognitiveEngine(db)
    activated = {"rule-seed": 1, "rule-seed-two": 1}
    historical = engine._find_relevant_rules([], "snapshot", activated, scope=SearchScope(project="snapshot", as_of=T1))
    assert [row["id"] for row in historical] == ["old-rule"]
    current = engine._find_relevant_rules([], "snapshot", {"rule-seed": 1})
    assert [row["id"] for row in current] == ["new-rule"]
    unscoped = engine._find_relevant_rules([], None, {"rule-seed": 1})
    assert len(unscoped) == 5
    assert all(row["id"].startswith("foreign-rule-") for row in unscoped)


def test_snapshot_cognitive_failures_filter_before_limit(snapshot_server):
    from cognitive.engine import CognitiveEngine
    from memory_core.retrieval import SearchScope
    server, _, _, _ = snapshot_server
    db = server.store.db
    episodes = [("old", "snapshot", T1, T1, 0.1), ("late-import", "snapshot", T1, T3, 1)]
    episodes.extend((f"future-{index}", "snapshot", T3, T3, 1) for index in range(6))
    episodes.append(("foreign", "elsewhere", T1, T1, 1))
    for identity, project, timestamp, created_at, impact in episodes:
        db.execute("INSERT INTO episodes(id,session_id,project,timestamp,created_at,narrative,outcome,concepts,impact_score) VALUES (?,'snapshot-test',?,?,?,?,'failure','[\"routing\"]',?)", (identity, project, timestamp, created_at, identity, impact))
    db.commit()
    engine = CognitiveEngine(db)
    context = engine.on_query("routing", "snapshot", scope=SearchScope(project="snapshot", as_of=T1))
    assert [row["id"] for row in context["past_failures"]] == ["old"]
    current = engine._find_past_failures(["routing"], "snapshot")
    assert len(current) == 5
    assert all(row["id"] != "foreign" for row in current)


def test_kg_at_matches_snapshot_replacement_and_deletion_boundaries(snapshot_server):
    server, _, _, _ = snapshot_server

    def call(tool, arguments):
        result, is_error = asyncio.run(server._call_tool_impl(tool, arguments))
        assert not is_error
        return json.loads(result[0].text)

    for value, instant in (("cobalt", T1), ("amber", T2), ("jade", T3)):
        call("kg_add_fact", {"subject": "routing", "predicate": "uses", "object": value, "project": "snapshot", "valid_from": instant})
    call("kg_add_fact", {"subject": "old-endpoint", "predicate": "uses", "object": "obsolete", "project": "snapshot", "valid_from": T1})
    call("kg_add_fact", {"subject": "foreign", "predicate": "uses", "object": "other", "project": "elsewhere", "valid_from": T1})
    invalidation = {"subject": "old-endpoint", "predicate": "uses", "object": "obsolete", "project": "snapshot", "at": TD}
    assert call("kg_invalidate_fact", invalidation)["closed"] == 1
    assert call("kg_invalidate_fact", invalidation)["closed"] == 0
    assert call("kg_invalidate_fact", {**invalidation, "subject": "routing", "object": "jade", "at": T1})["closed"] == 0
    for instant, expected in ((T0, set()), (T1, {"cobalt", "obsolete"}), (T2, {"amber", "obsolete"}),
                              (TD, {"amber"}), (T3, {"jade"}), ("2026-01-01T04:00:00+03:00", {"cobalt", "obsolete"}),
                              (None, {"jade"})):
        arguments = {"project": "snapshot"}
        if instant is not None:
            arguments["timestamp"] = instant
        payload = call("kg_at", arguments)
        assert {row["object"] for row in payload["assertions"]} == expected


@pytest.mark.parametrize("space,content", [
    ("code", "def cobalt_dispatch(value):\n    return value + 1\n"),
    ("config", '{"cobalt": "dispatch", "retries": 3}'),
    ("log", "2026-01-01 00:00:00 ERROR: cobalt dispatch failed\n2026-01-01 00:00:01 INFO: cobalt retry started"),
])
def test_snapshot_nontext_spaces_use_real_local_vectors(snapshot_server, monkeypatch, space, content):
    server, _, (first, _, _), _ = snapshot_server
    monkeypatch.setenv(f"MEMORY_{space.upper()}_EMBED_MODEL", "")
    db = server.store.db
    db.execute("UPDATE knowledge SET content=? WHERE id=?", (content, first))
    db.commit()
    for _ in range(2):
        result = server.recall.search("cobalt", project="snapshot", as_of=T1, embedding_space=space, limit=1)
        assert _ids(result) == {first}
        assert result["semantic_diagnostics"] == []
        cached = db.execute("SELECT embedding_space,embed_model FROM snapshot_embedding_cache WHERE knowledge_id=?", (first,)).fetchone()
        assert cached[0] == space
        assert cached[1] == server.store._active_embed_model_name()


def test_snapshot_unavailable_code_provider_keeps_lexical_results(snapshot_server, monkeypatch):
    server, _, (first, _, _), _ = snapshot_server
    monkeypatch.setenv("MEMORY_CODE_EMBED_MODEL", "")
    db = server.store.db
    db.execute("UPDATE knowledge SET content='def cobalt_dispatch(value):\n    return value + 1\n' WHERE id=?", (first,))
    db.commit()
    initial = server.recall.search("cobalt dispatch", project="snapshot", as_of=T1, embedding_space="code", limit=1)
    assert _ids(initial) == {first}
    monkeypatch.setenv("MEMORY_CODE_EMBED_MODEL", "tam-test-unavailable-model")
    result = server.recall.search("cobalt dispatch", project="snapshot", as_of=T1, embedding_space="code", limit=1)
    assert _ids(result) == {first}
    assert any(item["space"] == "code" and item["code"] == "embed_failed" for item in result["semantic_diagnostics"])


def test_snapshot_code_fallback_recovers_to_requested_space(snapshot_server, monkeypatch):
    server, _, (first, _, _), _ = snapshot_server
    db = server.store.db
    db.execute("UPDATE knowledge SET content='def cobalt_dispatch(value):\n    return value + 1\n' WHERE id=?", (first,))
    db.commit()
    monkeypatch.setenv("MEMORY_CODE_EMBED_MODEL", "tam-test-unavailable-model")
    result = server.recall.search("cobalt", project="snapshot", as_of=T1, embedding_space="code", limit=1)
    assert _ids(result) == set()
    assert result["semantic_diagnostics"]
    assert db.execute("SELECT embedding_space FROM snapshot_embedding_cache WHERE knowledge_id=?", (first,)).fetchone()[0] == "text"
    monkeypatch.setenv("MEMORY_CODE_EMBED_MODEL", "")
    recovered = server.recall.search("cobalt", project="snapshot", as_of=T1, embedding_space="code", limit=1)
    assert _ids(recovered) == {first}
    assert recovered["semantic_diagnostics"] == []
    assert db.execute("SELECT embedding_space FROM snapshot_embedding_cache WHERE knowledge_id=?", (first,)).fetchone()[0] == "code"


def test_snapshot_provider_failure_preserves_fts_and_is_not_cached(snapshot_server, monkeypatch):
    server, _, (first, _, _), _ = snapshot_server
    store = server.store
    store._restore_primary_embedder()

    def unavailable(texts):
        raise RuntimeError("Test provider unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(store, "embed", unavailable)
        result = server.recall.search("cobalt", project="snapshot", as_of=T1, limit=1)
    assert _ids(result) == {first}
    assert any(item["space"] == "text" for item in result["semantic_diagnostics"])
    assert store.db.execute("SELECT count(*) FROM snapshot_embedding_cache").fetchone()[0] == 0
    recovered = server.recall.search("cobalt", project="snapshot", as_of=T1, limit=1)
    assert _ids(recovered) == {first}
    assert recovered["semantic_diagnostics"] == []
    assert store.db.execute("SELECT count(*) FROM snapshot_embedding_cache").fetchone()[0] > 0


def test_snapshot_cross_encoder_context_excludes_future_neighbors(snapshot_server):
    from memory_core.cross_rerank import session_window_texts
    from memory_core.retrieval import SearchScope
    server, _, (first, _, third), deleted = snapshot_server
    db = server.store.db
    records = [dict(db.execute("SELECT * FROM knowledge WHERE id=?", (kid,)).fetchone()) for kid in (first, third)]
    texts = session_window_texts(db, records, side_chars=1000, scope=SearchScope(project="snapshot", as_of=T1))
    assert set(texts) == {first}
    assert "obsolete endpoint" in texts[first]
    assert "jade" not in texts[first]
    assert "amber" not in texts[first]
    assert "another project" not in texts[first]
    historical = dict(db.execute("SELECT * FROM knowledge WHERE id=?", (deleted,)).fetchone())
    reverse = session_window_texts(db, [historical], side_chars=1000, scope=SearchScope(project="snapshot", as_of=T1))
    assert "uses cobalt" in reverse[deleted]
