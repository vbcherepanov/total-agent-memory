"""Embedding identity: which model made a vector, which vectors are stale, how spaces are fused."""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memory_core.embedding_identity import (  # noqa: E402
    STALE_MODEL_SUFFIX,
    EmbedIdentity,
    FingerprintState,
    check_fingerprint,
    cosine,
    fastembed_pooling,
    record_fingerprint,
    retire_group,
    stale_ids,
    reconciliation_ids,
    embedding_coverage,
)
from tests.pg_store_support import store_backend  # noqa: E402,F401 — fixture

MODEL = "test-model"
OLD_MODEL = "retired-model"


def test_reconciliation_finds_missing_vectors_and_is_resumable(store):
    for kid in range(1, 5):
        seed(store, kid, [1.0, 0.0])
    store.db.execute("DELETE FROM embeddings WHERE knowledge_id IN (1,2,3)")
    store.db.execute("UPDATE knowledge SET status='superseded' WHERE id=3")
    store.db.commit()
    assert reconciliation_ids(store.db, {'text': MODEL}, limit=1) == [1]
    store._embedding_backfill().enqueue(1)
    assert reconciliation_ids(store.db, {'text': MODEL}, limit=1) == [2]
    store._embedding_backfill().enqueue(2)
    assert reconciliation_ids(store.db, {'text': MODEL}) == []
    coverage = embedding_coverage(store.db, {'text': MODEL})
    assert coverage.active == 3
    assert coverage.missing == 2
    assert coverage.pending == 2
    assert coverage.unqueued == 0


def test_coverage_distinguishes_missing_unknown_stale_and_dead_letter(store):
    seed(store, 1, [1.0, 0.0])
    seed(store, 2, [1.0, 0.0], model='auto')
    seed(store, 3, [1.0, 0.0], model=OLD_MODEL)
    store.db.execute('DELETE FROM embeddings WHERE knowledge_id=1')
    store.db.commit()
    store._embedding_backfill().enqueue(3)
    store.db.execute("UPDATE embedding_pending SET status='failed' WHERE knowledge_id=3")
    store.db.commit()
    coverage = embedding_coverage(store.db, {'text': MODEL})
    assert (coverage.missing, coverage.unknown, coverage.stale, coverage.failed, coverage.unqueued) == (1,1,1,1,2)
    assert reconciliation_ids(store.db, {'text': MODEL}) == [1, 2]


def test_startup_reconciliation_enqueues_legacy_missing_vectors(store):
    seed(store, 1, [1.0, 0.0])
    store.db.execute('DELETE FROM embeddings WHERE knowledge_id=1')
    store.db.commit()
    assert store.enqueue_stale_vectors(force=True) == 1
    assert store.enqueue_stale_vectors(force=True) == 0
    assert store.db.execute('SELECT knowledge_id FROM embedding_pending').fetchone()[0] == 1


@pytest.fixture
def store(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    for flag in ("MEMORY_QUALITY_GATE_ENABLED", "MEMORY_CONTRADICTION_DETECT_ENABLED",
                 "MEMORY_OUTBOX_ENABLED", "MEMORY_EPISODIC_ENABLED"):
        monkeypatch.setenv(flag, "false")
    import server as srv

    monkeypatch.setattr(srv, "MEMORY_DIR", tmp_path)
    s = srv.Store(database=store_backend)
    monkeypatch.setattr(s, "_active_embed_model_name", lambda: MODEL)
    s._embed_mode = "fastembed"
    yield s
    s.db.close()


def seed(store, kid, vector, *, model=MODEL, space="text", project="p"):
    store.db.execute(
        "INSERT INTO knowledge (id, session_id, type, content, project, status, created_at) "
        "VALUES (?, 's', 'fact', ?, ?, 'active', '2026-04-27T00:00:00Z')", (kid, f"row-{kid}", project))
    store._upsert_embedding(kid, list(vector), model, embedding_space=space)
    store.db.commit()


def test_spaces_that_share_a_model_are_ranked_together_by_cosine(store, monkeypatch):
    for kid, vector in {1: [1.0, 0.0], 2: [0.9, 0.1], 3: [0.8, 0.2]}.items():
        seed(store, kid, vector)
    for kid, vector in {4: [0.5, 0.5], 5: [0.4, 0.6]}.items():
        seed(store, kid, vector, space="config")
    monkeypatch.setattr(store, "embed", lambda texts: [[1.0, 0.0]])
    store._v11_embed_provider = SimpleNamespace(active_model=lambda space: MODEL)

    hits = store._search_spaces("query", project="p", limit=3)

    assert [kid for kid, _ in hits] == [1, 2, 3]
    assert all(score > 0.5 for _, score in hits)


def test_models_that_differ_are_fused_by_rank_with_cosine_scores(store, monkeypatch):
    seed(store, 1, [1.0, 0.0])
    seed(store, 2, [0.0, 1.0, 0.0], model="code-model", space="code")
    monkeypatch.setattr(store, "embed", lambda texts: [[1.0, 0.0]])
    store._v11_embed_provider = SimpleNamespace(
        active_model=lambda space: "code-model" if space == "code" else MODEL,
        embed_query=lambda query, space: [0.0, 1.0, 0.0])

    hits = dict(store._search_spaces("query", project="p", limit=5))

    assert set(hits) == {1, 2}
    assert all(0.99 <= score <= 1.0 for score in hits.values())


def test_fallback_vectors_carry_the_fallback_model(store, monkeypatch):
    import server as srv

    monkeypatch.setenv("MEMORY_ALLOW_OLLAMA_IN_HOT_PATH", "true")
    monkeypatch.setattr(store, "_fastembed_embed", lambda texts: None)
    monkeypatch.setattr(store, "_check_ollama", lambda: True)
    monkeypatch.setattr(store, "_ollama_embed", lambda texts: [[0.5, 0.5, 0.5] for _ in texts])

    vectors, identity = store.embed_identified(["hello"])
    record, error = store._embed_for_record("hello", "", "auto")

    assert vectors == [[0.5, 0.5, 0.5]]
    assert (identity.model, identity.provider) == (srv.OLLAMA_EMBED_MODEL, "ollama")
    assert error is None
    assert (record["model"], record["provider"]) == (srv.OLLAMA_EMBED_MODEL, "ollama")
    assert record["model"] != MODEL


def test_primary_vectors_carry_the_configured_model(store, monkeypatch):
    monkeypatch.setattr(store, "_fastembed_embed", lambda texts: [[0.1, 0.2] for _ in texts])

    record, _ = store._embed_for_record("hello", "", "auto")

    assert (record["model"], record["provider"]) == (MODEL, "fastembed")


def test_fallback_vector_is_not_served_from_the_l2_cache_as_the_configured_model(store, monkeypatch, tmp_path):
    import server as srv
    from cache_layer import L2EmbeddingCache

    l2 = L2EmbeddingCache(db_path=tmp_path / "l2.db", enabled=True)
    store.v9_cache = SimpleNamespace(l2=l2, embed_get=l2.get, embed_set=l2.set)
    monkeypatch.setenv("MEMORY_ALLOW_OLLAMA_IN_HOT_PATH", "true")
    monkeypatch.setattr(store, "_fastembed_embed", lambda texts: None)
    monkeypatch.setattr(store, "_check_ollama", lambda: True)
    monkeypatch.setattr(store, "_ollama_embed", lambda texts: [[0.5, 0.5, 0.5] for _ in texts])

    store.embed(["hello"])

    assert l2.get("hello", expected_model=MODEL) is None
    assert l2.get("hello", expected_model=srv.OLLAMA_EMBED_MODEL) == [0.5, 0.5, 0.5]
    l2.close()


def test_stale_ids_lists_only_records_of_other_models(store):
    seed(store, 1, [1.0, 0.0])
    seed(store, 2, [1.0, 0.0], model=OLD_MODEL)
    seed(store, 3, [1.0, 0.0], model=OLD_MODEL, space="config")
    store.db.execute("UPDATE knowledge SET status='superseded' WHERE id=3")

    assert stale_ids(store.db, {"text": MODEL, "config": MODEL}) == [2]
    assert stale_ids(store.db, {}) == []


def test_stale_vectors_are_queued_for_reembedding(store):
    seed(store, 1, [1.0, 0.0])
    seed(store, 2, [1.0, 0.0], model=OLD_MODEL)

    assert store.enqueue_stale_vectors(force=True) == 1
    assert store.enqueue_stale_vectors(force=True) == 0
    assert [row[0] for row in store.db.execute("SELECT knowledge_id FROM embedding_pending")] == [2]


def test_stale_scan_does_nothing_while_a_fallback_serves_embeds(store):
    seed(store, 2, [1.0, 0.0], model=OLD_MODEL)
    store._embed_mode = "st"

    assert store.enqueue_stale_vectors(force=True) == 0


def test_stale_scan_is_rate_limited(store):
    seed(store, 2, [1.0, 0.0], model=OLD_MODEL)
    store.enqueue_stale_vectors(force=True)
    seed(store, 3, [1.0, 0.0], model=OLD_MODEL)

    assert store.enqueue_stale_vectors() == 0
    assert store.enqueue_stale_vectors(force=True) == 1


def test_drain_replaces_a_stale_vector_and_keeps_its_space(store, monkeypatch):
    seed(store, 2, [1.0, 0.0], model=OLD_MODEL, space="text")
    store.db.execute("UPDATE embeddings SET content_type='code', language='py' WHERE knowledge_id=2")
    store.db.commit()
    monkeypatch.setattr(store, "_fastembed_embed", lambda texts: [[0.0, 1.0] for _ in texts])
    monkeypatch.setattr(store, "_expected_models", lambda: {"text": MODEL})
    monkeypatch.setattr(store, "_embed_for_record", lambda *a, **k: (
        {"vector": [0.0, 1.0], "model": MODEL, "provider": "fastembed", "space": "text",
         "content_type": "code", "language": "py"}, None))
    store.enqueue_stale_vectors(force=True)

    counts = store.drain_embedding_backfill()

    assert counts["embedded"] == 1
    model, space, content_type, language = store.db.execute(
        "SELECT embed_model, embedding_space, content_type, language FROM embeddings WHERE knowledge_id=2").fetchone()
    assert (model, space, content_type, language) == (MODEL, "text", "code", "py")
    assert store.db.execute("SELECT COUNT(*) FROM embedding_pending").fetchone()[0] == 0


def test_drain_leaves_stale_vectors_alone_during_a_fallback(store, monkeypatch):
    seed(store, 2, [1.0, 0.0], model=OLD_MODEL)
    store.enqueue_stale_vectors(force=True)
    store._embed_mode = "st"
    monkeypatch.setattr(store, "_embed_for_record", lambda *a, **k: pytest.fail("must not re-embed on a fallback"))

    store.drain_embedding_backfill()

    assert store.db.execute("SELECT embed_model FROM embeddings WHERE knowledge_id=2").fetchone()[0] == OLD_MODEL


def test_drain_does_not_loop_when_the_new_vector_is_still_not_the_spaces_model(store, monkeypatch):
    seed(store, 2, [1.0, 0.0], model=OLD_MODEL)
    monkeypatch.setattr(store, "_expected_models", lambda: {"text": MODEL})
    monkeypatch.setattr(store, "_embed_for_record", lambda *a, **k: (
        {"vector": [1.0, 0.0], "model": "other", "provider": "st", "space": "text",
         "content_type": "text", "language": None}, None))
    store.enqueue_stale_vectors(force=True)

    counts = store.drain_embedding_backfill()

    assert counts["retried"] == 1
    assert store.db.execute("SELECT embed_model FROM embeddings WHERE knowledge_id=2").fetchone()[0] == OLD_MODEL


def test_provider_down_reports_a_fallback_not_a_model_change(store, monkeypatch):
    import server as srv

    seed(store, 1, [1.0, 0.0], model=srv.FASTEMBED_MODEL)
    monkeypatch.undo()  # keep the real _active_embed_model_name for the st fallback
    store._embed_mode = "st"
    monkeypatch.setattr(store, "_restore_primary_embedder", lambda: None)
    monkeypatch.setattr(store, "embed", lambda texts: [[1.0, 0.0]])

    assert store._search_spaces("query", project="p") == []

    (diagnostic,) = store._semantic_diagnostics
    assert diagnostic["code"] == "provider_fallback"
    assert diagnostic["count"] == 1
    assert "re-embed required" not in diagnostic["error"]


def test_retired_model_reports_stale_vectors(store, monkeypatch):
    seed(store, 1, [1.0, 0.0], model=OLD_MODEL)
    monkeypatch.setattr(store, "embed", lambda texts: [[1.0, 0.0]])

    store._search_spaces("query", project="p")

    (diagnostic,) = store._semantic_diagnostics
    assert (diagnostic["code"], diagnostic["count"]) == ("stale_vectors", 1)


def test_failed_query_embedding_is_not_reported_as_a_model_change(store, monkeypatch):
    seed(store, 1, [1.0, 0.0])
    monkeypatch.setattr(store, "embed", lambda texts: None)

    assert store._search_spaces("query", project="p") == []

    (diagnostic,) = store._semantic_diagnostics
    assert diagnostic["code"] == "embed_failed"
    assert "re-embed" not in diagnostic["error"]


def test_group_matching_the_running_fallback_stays_searchable(store, monkeypatch):
    """The main group waits for its provider; vectors of the fallback model are still searched."""
    import server as srv

    seed(store, 1, [1.0, 0.0], model=srv.FASTEMBED_MODEL)
    seed(store, 2, [1.0, 0.0], model="fallback-model")
    monkeypatch.undo()
    store._embed_mode = "st"
    monkeypatch.setattr(store, "_restore_primary_embedder", lambda: None)
    monkeypatch.setattr(store, "_active_embed_model_name", lambda: "fallback-model")
    monkeypatch.setattr(store, "embed", lambda texts: [[1.0, 0.0]])

    hits = store._search_spaces("query", project="p")

    assert [kid for kid, _ in hits] == [2]
    assert [d["code"] for d in store._semantic_diagnostics] == ["provider_fallback"]


def test_failed_fastembed_load_is_retried_after_the_cooldown(store, monkeypatch):
    import server as srv
    from memory_core import fastembed_loader

    loaded = object()
    monkeypatch.setattr(fastembed_loader, "load_model", lambda *a, **k: loaded)
    monkeypatch.setattr(srv, "HAS_FASTEMBED", True)
    store._fastembed_model = False
    store._fastembed_failed_at = time.monotonic()
    assert store.fastembed is None

    store._fastembed_failed_at = time.monotonic() - srv.FASTEMBED_RETRY_SECONDS - 1
    assert store.fastembed is loaded


def test_store_returns_to_fastembed_when_the_model_loads_again(store, monkeypatch):
    import server as srv

    monkeypatch.setattr(srv, "HAS_FASTEMBED", True)
    monkeypatch.setattr(type(store), "fastembed", property(lambda self: object()))
    store._embed_mode = "st"

    store._restore_primary_embedder()

    assert store._embed_mode == "fastembed"


def test_a_cloud_mode_is_not_replaced_by_fastembed(store, monkeypatch):
    import server as srv

    monkeypatch.setattr(srv, "HAS_FASTEMBED", True)
    monkeypatch.setattr(type(store), "fastembed", property(lambda self: object()))
    store._embed_mode = "openai"

    store._restore_primary_embedder()

    assert store._embed_mode == "openai"


# -- fingerprints -------------------------------------------------------------


def test_fingerprint_states(store):
    identity = EmbedIdentity(MODEL, "fastembed", "mean", "fastembed 0.8.0")

    assert check_fingerprint(store.db, "text", MODEL, [1.0, 0.0]) is FingerprintState.NEW
    record_fingerprint(store.db, "text", identity, [1.0, 0.0])
    assert check_fingerprint(store.db, "text", MODEL, [1.0, 0.00001]) is FingerprintState.SAME
    assert check_fingerprint(store.db, "text", MODEL, [0.0, 1.0]) is FingerprintState.CHANGED
    assert check_fingerprint(store.db, "text", MODEL, [1.0, 0.0, 0.0]) is FingerprintState.CHANGED


def test_retire_group_relabels_only_that_group(store):
    seed(store, 1, [1.0, 0.0])
    seed(store, 2, [1.0, 0.0], model="other")

    assert retire_group(store.db, "text", MODEL) == 1

    models = dict(store.db.execute("SELECT knowledge_id, embed_model FROM embeddings"))
    assert models == {1: MODEL + STALE_MODEL_SUFFIX, 2: "other"}


def test_changed_recipe_retires_the_vectors_and_queues_them(store, monkeypatch):
    seed(store, 1, [1.0, 0.0])
    monkeypatch.setattr(store, "_fastembed_embed", lambda texts: [[1.0, 0.0]])
    store._verify_embedding_fingerprint()
    assert store.db.execute("SELECT embed_model FROM embeddings WHERE knowledge_id=1").fetchone()[0] == MODEL

    monkeypatch.setattr(store, "_fastembed_embed", lambda texts: [[0.0, 1.0]])
    store._verify_embedding_fingerprint()

    assert store.db.execute("SELECT embed_model FROM embeddings WHERE knowledge_id=1").fetchone()[0] == MODEL + STALE_MODEL_SUFFIX
    assert store.enqueue_stale_vectors(force=True) == 1
    assert check_fingerprint(store.db, "text", MODEL, [0.0, 1.0]) is FingerprintState.SAME


def test_unchanged_recipe_keeps_the_vectors(store, monkeypatch):
    seed(store, 1, [1.0, 0.0])
    monkeypatch.setattr(store, "_fastembed_embed", lambda texts: [[1.0, 0.0]])

    store._verify_embedding_fingerprint()
    store._verify_embedding_fingerprint()

    assert store.db.execute("SELECT embed_model FROM embeddings WHERE knowledge_id=1").fetchone()[0] == MODEL


def test_pooling_follows_the_fastembed_version():
    assert fastembed_pooling("0.5.1") == "cls"
    assert fastembed_pooling("0.6.0") == "mean"
    assert fastembed_pooling("0.8.0") == "mean"


def test_cosine_of_mismatched_vectors_is_zero():
    assert cosine([1.0], [1.0, 0.0]) == 0.0
    assert cosine([], []) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


# -- text model resolution --------------------------------------------------------


def test_one_resolver_names_the_text_model(monkeypatch):
    from embed_provider import FastEmbedProvider
    from memory_core.embedding_spaces import model_for_space, resolve_text_model

    for name in ("FASTEMBED_MODEL", "MEMORY_EMBED_MODEL", "MEMORY_TEXT_EMBED_MODEL", "MEMORY_EMBED_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORY_TEXT_EMBED_MODEL", "text-model")
    assert resolve_text_model() == "text-model"

    monkeypatch.setenv("MEMORY_EMBED_MODEL", "embed-model")
    assert resolve_text_model() == "embed-model"

    monkeypatch.setenv("FASTEMBED_MODEL", "legacy-model")
    assert resolve_text_model() == "legacy-model"
    assert model_for_space("text") == "legacy-model"
    assert FastEmbedProvider().model_name == "legacy-model"


def test_cloud_provider_model_is_not_a_text_space_model(monkeypatch):
    from memory_core.embedding_spaces import resolve_text_model

    monkeypatch.delenv("FASTEMBED_MODEL", raising=False)
    monkeypatch.setenv("MEMORY_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("MEMORY_EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("MEMORY_TEXT_EMBED_MODEL", "text-model")

    assert resolve_text_model() == "text-model"


# -- HyDE -----------------------------------------------------------------------


def test_hyde_embeds_with_the_callers_encoder(monkeypatch):
    import reranker

    monkeypatch.setattr(reranker, "_ollama_generate", lambda *a, **k: "an answer")
    monkeypatch.setattr("config.has_llm", lambda: True)
    monkeypatch.setattr(reranker, "_provider_embed", lambda text: pytest.fail("provider model differs from the store's"))
    seen = []

    def encode(texts):
        seen.append(texts)
        return [[0.25, 0.75]]

    assert reranker.hyde_expand("question", "p", embed_fn=encode) == [0.25, 0.75]
    assert seen == [["an answer"]]


def test_hyde_without_vector_returns_none(monkeypatch):
    import reranker

    monkeypatch.setattr(reranker, "_ollama_generate", lambda *a, **k: "an answer")
    monkeypatch.setattr("config.has_llm", lambda: True)

    assert reranker.hyde_expand("question", embed_fn=lambda texts: None) is None


def test_provider_failure_does_not_fall_back_to_another_model(monkeypatch):
    import reranker

    class Broken:
        def embed(self, texts):
            raise RuntimeError("down")

    monkeypatch.setattr(reranker, "_embed_provider_cache", Broken())
    monkeypatch.setattr(reranker, "_ollama_embed", lambda *a, **k: pytest.fail("Ollama vectors are another model"))

    assert reranker._provider_embed("text") is None


def test_hyde_embed_rejects_a_fallback_answer(store, monkeypatch):
    monkeypatch.setattr(store, "embed", lambda texts: [[1.0, 0.0]])
    store._embed_identity_slot().value = EmbedIdentity("fallback-model", "st")

    assert store._hyde_embed(["x"]) is None

    store._embed_identity_slot().value = EmbedIdentity(MODEL, "fastembed")
    assert store._hyde_embed(["x"]) == [[1.0, 0.0]]


# -- migration ----------------------------------------------------------------------


def test_fingerprint_migration_is_additive_and_repeatable():
    db = sqlite3.connect(":memory:")
    script = (Path(__file__).parents[1] / "migrations" / "044_embedding_fingerprints.sql").read_text(encoding="utf-8")
    db.executescript(script)
    db.executescript(script)
    columns = [row[1] for row in db.execute("PRAGMA table_info(embedding_fingerprints)")]
    assert columns == ["space", "model", "provider", "dim", "pooling", "library", "probe_vector", "recorded_at"]
