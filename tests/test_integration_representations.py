"""Integration: memory_save → representations_queue; reflection generates views."""

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


def _fake_embedder(text: str) -> list[float]:
    h = hashlib.md5(text.encode()).digest()
    return [b / 255.0 for b in h[:8]]


def test_save_enqueues_representations(store):
    store.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) VALUES ('s1', '2026-04-14T00:00:00Z', 'demo', 'open')"
    )
    store.db.commit()
    rid, *_save = store.save_knowledge(
        sid="s1", content="content", ktype="fact", project="demo"
    )

    row = store.db.execute(
        "SELECT status FROM representations_queue WHERE knowledge_id=?", (rid,)
    ).fetchone()
    assert row is not None
    assert row["status"] == "pending"


def test_reflection_generates_representations(store, monkeypatch):
    from reflection.agent import ReflectionAgent
    from memory_core.embedding_identity import EmbedIdentity
    import representations

    store.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) VALUES ('s1', '2026-04-14T00:00:00Z', 'demo', 'open')"
    )
    store.db.commit()

    long_content = (
        "A comprehensive production guide about Go microservices built with "
        "gRPC transport and PostgreSQL 18 as backing store. It covers health "
        "checks, Prometheus metrics, structured slog logging, OpenTelemetry "
        "tracing, Docker Compose v2 orchestration, GitHub Actions pipelines, "
        "and safe database migrations with concurrent index creation. "
        "Additional sections discuss auth via JWT, rate limiting, and caching."
    )
    rid, *_save = store.save_knowledge(
        sid="s1", content=long_content, ktype="fact", project="demo"
    )

    # Stub the LLM in representations module
    def fake_llm(prompt, **_):
        p = prompt.lower()
        if "summary" in p or "summarize" in p:
            return "Short summary of Go microservices guide."
        if "keyword" in p:
            return "go, grpc, postgres, microservices"
        if "question" in p:
            return "What services does this cover?\nHow does auth work?"
        return ""

    monkeypatch.setattr(representations, "_llm_complete", fake_llm)

    # Also stub triple extractor + deep_enricher to keep run_full fast and offline
    import ingestion.extractor as extractor_mod
    monkeypatch.setattr(
        extractor_mod.ConceptExtractor, "extract_and_link",
        lambda self, text, knowledge_id=None, deep=False: {"relations": [], "entities": []},
    )
    import deep_enricher
    monkeypatch.setattr(deep_enricher, "_llm_complete", lambda *a, **kw: "")

    identity = EmbedIdentity("fake", "declared")
    agent = ReflectionAgent(
        store.db, embedder=_fake_embedder, embed_identity=lambda: identity,
    )
    report = asyncio.run(agent.run_full())

    assert report["representations"]["processed"] >= 1

    # knowledge_representations should have raw + summary + keywords + questions
    kinds = {
        r["representation"]
        for r in store.db.execute(
            "SELECT representation FROM knowledge_representations WHERE knowledge_id=?",
            (rid,),
        ).fetchall()
    }
    assert "raw" in kinds
    assert "summary" in kinds
    assert "keywords" in kinds
    assert "questions" in kinds
    rows = store.db.execute(
        "SELECT embed_identity_key FROM knowledge_representations WHERE knowledge_id=?",
        (rid,),
    ).fetchall()
    assert all(row["embed_identity_key"] == identity.key(8) for row in rows)
