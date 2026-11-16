import math

from memory_core.retrieval import SearchScope
from memory_core.snapshot_semantic import SnapshotSemanticIndex, SnapshotVector
from tests.pg_store_support import store_backend  # noqa: F401
from tests.test_recall_snapshot import T3, snapshot_server  # noqa: F401


def test_snapshot_fuses_incompatible_models_by_rank_before_limit(snapshot_server):
    server, save, (_first, _second, text_top), _deleted = snapshot_server
    text_second = save("Routing dispatch text runner up")
    code_top = save("Routing dispatch code model winner")
    cosine = {text_top: 0.99, text_second: 0.98, code_top: 0.1}

    def space(record):
        return "code" if record["id"] == code_top else "text"

    def vector(values, model):
        return SnapshotVector(values, model, "fixture", model, model + ":2")

    def encode(record):
        score = cosine[record["id"]]
        return vector([score, math.sqrt(1 - score * score)], space(record))

    index = SnapshotSemanticIndex(server.store.db, encode, lambda _query, model: vector([1.0, 0.0], model), space)
    scope = SearchScope(project="snapshot", as_of=T3)
    hits = index.search("routing", scope, 2)
    assert [identity for identity, _score in hits] == [text_top, code_top]
    assert index.search("routing", scope, 2) == hits


def test_recall_keeps_model_fusion_order(snapshot_server, monkeypatch):
    server, save, (_first, _second, text_top), _deleted = snapshot_server
    code_top = save("Routing dispatch additional code winner")
    monkeypatch.setattr(server.store, "_search_spaces", lambda *args, **kwargs: [(code_top, 0.1), (text_top, 0.99)])
    result = server.recall.search("Routing dispatch", "snapshot", as_of=T3, _explain=True,
                                  tier_weights={"fts": 0, "semantic": 1, "fuzzy": 0, "graph": 0})
    semantic = result["_explain"]["semantic"]
    assert [row["id"] for row in semantic] == [code_top, text_top]
