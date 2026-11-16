"""Test the fast `run_drain` scope — drains queues without digest/synthesize."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# These tests stub the LLM seam; `llm_enabled` skips the has_llm() probe
# so they do not depend on a live Ollama being present on the machine.
pytestmark = pytest.mark.usefixtures("llm_enabled")


@pytest.fixture
def drain_db():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    root = Path(__file__).parent.parent
    conn.executescript((root / "migrations" / "001_v5_schema.sql").read_text(encoding="utf-8"))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT,
            project TEXT DEFAULT 'general', status TEXT DEFAULT 'open',
            summary TEXT, log_count INTEGER DEFAULT 0, branch TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT, type TEXT, project TEXT DEFAULT 'general',
            tags TEXT DEFAULT '[]', status TEXT DEFAULT 'active',
            confidence REAL DEFAULT 1.0, created_at TEXT, session_id TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS embeddings (
            knowledge_id INTEGER PRIMARY KEY,
            binary_vector BLOB NOT NULL, float32_vector BLOB NOT NULL,
            embed_model TEXT NOT NULL, embed_dim INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_merges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merged_knowledge_id INTEGER NOT NULL,
            source_ids TEXT NOT NULL, rationale TEXT, created_at TEXT NOT NULL
        );
        """
    )
    for m in ("002_multi_representation", "003_triple_extraction_queue",
              "004_deep_enrichment", "005_representations_queue"):
        conn.executescript((root / "migrations" / f"{m}.sql").read_text(encoding="utf-8"))
    yield conn
    conn.close()


def test_run_drain_only_runs_phases_3_5_6(drain_db, monkeypatch):
    from reflection.agent import ReflectionAgent
    from triple_extraction_queue import TripleExtractionQueue
    from deep_enrichment_queue import DeepEnrichmentQueue
    from representations_queue import RepresentationsQueue

    kid = drain_db.execute(
        "INSERT INTO knowledge (content, type, project, created_at) "
        "VALUES ('quick test content', 'fact', 'demo', '2026-04-14T00:00:00Z')"
    ).lastrowid
    drain_db.commit()
    TripleExtractionQueue(drain_db).enqueue(kid)
    DeepEnrichmentQueue(drain_db).enqueue(kid)
    RepresentationsQueue(drain_db).enqueue(kid)

    # Stub heavy dependencies so no Ollama/FastEmbed involvement
    import ingestion.extractor as extractor_mod
    import deep_enricher, representations

    monkeypatch.setattr(
        extractor_mod.ConceptExtractor, "extract_and_link",
        lambda self, text, knowledge_id=None, deep=False: {"relations": []},
    )
    monkeypatch.setattr(deep_enricher, "_llm_complete", lambda *a, **kw: "")
    monkeypatch.setattr(representations, "_llm_complete", lambda *a, **kw: "")

    # Digest/synthesize classes must NOT be invoked — sentinel any call
    from reflection.digest import DigestPhase
    from reflection.synthesize import SynthesizePhase
    digest_calls, synth_calls = [], []
    monkeypatch.setattr(DigestPhase, "run", lambda self: digest_calls.append(1) or {})
    monkeypatch.setattr(SynthesizePhase, "run", lambda self, days=7: synth_calls.append(1) or {})

    from memory_core.embedding_identity import EmbedIdentity
    agent = ReflectionAgent(drain_db, embedder=lambda t: [0.1] * 8,
                            embed_identity=lambda: EmbedIdentity("fake", "declared"))
    report = asyncio.run(agent.run_drain())

    assert report["scope"] == "drain"
    assert "digest" not in report
    assert "synthesis" not in report
    assert report["triple_extraction"]["processed"] == 1
    assert report["deep_enrichment"]["processed"] == 1
    assert report["representations"]["processed"] == 1
    assert digest_calls == []
    assert synth_calls == []


def test_pick_scope_auto_prefers_drain():
    from tools.run_reflection import pick_scope

    assert pick_scope({"a": 0, "b": 0, "c": 0}) == "quick"
    assert pick_scope({"a": 1, "b": 1, "c": 1}) == "drain"
    assert pick_scope({"a": 100, "b": 100, "c": 100}) == "drain"  # always drain
    assert pick_scope({"a": 0}, override="full") == "full"
    assert pick_scope({"a": 99}, override="quick") == "quick"


def test_standalone_runner_propagates_embedding_identity(drain_db, tmp_path, monkeypatch):
    import sqlite3
    import sys
    import server
    import representations
    from tools import run_reflection
    from representations_queue import RepresentationsQueue
    from memory_core.embedding_identity import EmbedIdentity

    kid = drain_db.execute("INSERT INTO knowledge(content,type,project,created_at) VALUES ('Identity regression','fact','demo','2026-11-16')").lastrowid
    drain_db.commit()
    RepresentationsQueue(drain_db).enqueue(kid)
    with sqlite3.connect(tmp_path / 'memory.db') as saved:
        drain_db.backup(saved)

    class EmbedStore:
        def __init__(self):
            self.identity = None

        def embed(self, texts):
            self.identity = EmbedIdentity('fixture-model', 'fastembed')
            return [[0.1] * 8 for _ in texts]

        def last_embed_identity(self):
            return self.identity

    monkeypatch.setattr(server, 'Store', EmbedStore)
    monkeypatch.setattr(server, 'MEMORY_DIR', tmp_path)
    monkeypatch.setattr(run_reflection, '_resolve_memory_dir', lambda: tmp_path)
    monkeypatch.setattr(run_reflection, '_run_embedding_backfill', lambda store: None)
    monkeypatch.setattr(run_reflection, '_run_consolidation_sweep', lambda *args: None)
    monkeypatch.setattr(representations, 'generate_representations', lambda *args: {})
    monkeypatch.setattr(sys, 'argv', ['run_reflection', '--scope=drain', '--debounce=0'])
    assert run_reflection.main() == 0
    with sqlite3.connect(tmp_path / 'memory.db') as db:
        assert db.execute('SELECT status,attempts FROM representations_queue WHERE knowledge_id=?', (kid,)).fetchone() == ('done', 0)
        row = db.execute('SELECT embed_model,embed_identity_key FROM knowledge_representations WHERE knowledge_id=?', (kid,)).fetchone()
        assert row == ('fixture-model', EmbedIdentity('fixture-model', 'fastembed').key(8))


def test_missing_injected_identity_keeps_queue_retry_budget(drain_db):
    from reflection.agent import ReflectionAgent
    from representations_queue import RepresentationsQueue
    kid = drain_db.execute("INSERT INTO knowledge(content,type,project) VALUES ('Identity guard','fact','demo')").lastrowid
    drain_db.commit()
    RepresentationsQueue(drain_db).enqueue(kid)
    result = ReflectionAgent(drain_db, embedder=lambda text: [0.1] * 8)._run_representations()
    assert 'identity' in result['error']
    assert drain_db.execute('SELECT status,attempts FROM representations_queue WHERE knowledge_id=?', (kid,)).fetchone()[:] == ('pending', 0)


def test_ai_layer_wrapper_forwards_injected_identity(monkeypatch):
    from ai_layer import reflection
    from memory_core.embedding_identity import EmbedIdentity
    from reflection.agent import ReflectionAgent
    identity = lambda: EmbedIdentity('fixture-model', 'fastembed')

    async def run(agent):
        assert agent._embed_identity is identity
        return {'verified': True}

    monkeypatch.setattr(ReflectionAgent, 'run_full', run)
    result = asyncio.run(reflection.run_full(None, embedder=lambda text: [0.1] * 8, embed_identity=identity))
    assert result == {'verified': True}
