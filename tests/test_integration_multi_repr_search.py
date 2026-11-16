"""End-to-end integration: memory_save → queue → reflection → search uses multi_repr tier."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

# These tests stub the LLM seam; `llm_enabled` skips the has_llm() probe
# so they do not depend on a live Ollama being present on the machine.
pytestmark = pytest.mark.usefixtures("llm_enabled")


@pytest.fixture
def store(monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    (tmp_path / "blobs").mkdir(exist_ok=True)
    (tmp_path / "chroma").mkdir(exist_ok=True)
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    s = server.Store()
    yield s
    try:
        s.db.close()
    except Exception:
        pass


def _det_emb(text: str, dim: int = 8) -> list[float]:
    h = hashlib.md5(text.encode()).digest()
    return [b / 255.0 for b in h[:dim]]


def test_search_returns_multi_repr_hits_after_reflection(store, monkeypatch):
    """Full flow: save → reflection drains repr queue → search hits via multi_repr tier."""
    import representations
    import ingestion.extractor as extractor_mod
    import deep_enricher
    from reflection.agent import ReflectionAgent

    # Seed session
    store.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) VALUES ('s1', '2026-04-14T00:00:00Z', 'demo', 'open')"
    )
    store.db.commit()

    # Save — uses real embedder (fastembed loaded in Store)
    long_content = (
        "A Kubernetes operator pattern for managing Redis clusters. "
        "It uses custom resource definitions (CRDs) and a controller loop "
        "built with kubebuilder and controller-runtime. Reconciliation "
        "handles failover, master election, and backup scheduling automatically. "
        "Deployment happens via Helm charts with values for different environments."
    )
    rid, *_save = store.save_knowledge(
        sid="s1", content=long_content, ktype="solution", project="demo"
    )

    # Stub LLM calls in representations (summary/keywords/questions)
    def fake_repr_llm(prompt, **_):
        p = prompt.lower()
        if "summary" in p or "summarize" in p:
            return "Kubernetes operator for Redis cluster management with CRDs."
        if "keyword" in p:
            return "kubernetes, operator, redis, crd, controller"
        if "question" in p:
            return "How to reconcile Redis clusters?\nWhat are CRDs?"
        return ""

    monkeypatch.setattr(representations, "_llm_complete", fake_repr_llm)
    monkeypatch.setattr(deep_enricher, "_llm_complete", lambda *a, **kw: "")
    monkeypatch.setattr(
        extractor_mod.ConceptExtractor, "extract_and_link",
        lambda self, text, knowledge_id=None, deep=False: {"relations": []},
    )

    # Use the store's own embedder (so embed_dim matches query later)
    def store_embedder(text: str) -> list[float]:
        embs = store.embed([text])
        return embs[0] if embs else []

    agent = ReflectionAgent(store.db, embedder=store_embedder, embed_identity=store.last_embed_identity)
    report = asyncio.run(agent.run_full())
    assert report["representations"]["processed"] >= 1

    # Verify representations written
    rows = store.db.execute(
        "SELECT representation FROM knowledge_representations WHERE knowledge_id=?",
        (rid,),
    ).fetchall()
    kinds = {r["representation"] for r in rows}
    assert {"raw", "summary", "keywords", "questions"}.issubset(kinds)

    # Now search — multi_repr tier should fire and return the record
    import server as _srv
    recall = _srv.Recall(store)

    result = recall.search(
        query="redis cluster failover",
        project="demo",
        limit=10,
    )

    # Result should include our record, and at least one tier should be multi_repr
    all_items: list[dict] = []
    for group in result.get("results", {}).values():
        all_items.extend(group)
    found = [it for it in all_items if it.get("id") == rid]
    assert found, "record not in recall results"
    vias: list[str] = found[0].get("via", [])
    # We don't strictly require multi_repr to be the ONLY tier, but it should
    # be present as one of the contributing tiers once representations exist.
    assert "multi_repr" in vias
