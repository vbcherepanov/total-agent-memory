"""Shared fixtures for Claude Super Memory v5.0 test suite."""

import pytest
import sqlite3
import sys
from pathlib import Path

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture(autouse=True)
def _isolate_active_context_vault(tmp_path_factory, monkeypatch):
    """Redirect activeContext.md writes into a per-test tmp dir.

    Prevents the session_continuity markdown projection from writing into the
    real ~/Documents vault during test runs. Tests that care about the vault
    path override the env var themselves.
    """
    if "MEMORY_ACTIVECONTEXT_VAULT" not in __import__("os").environ:
        safe = tmp_path_factory.mktemp("active_ctx_vault")
        monkeypatch.setenv("MEMORY_ACTIVECONTEXT_VAULT", str(safe))
    yield


@pytest.fixture(autouse=True)
def _disable_quality_gate_in_tests(monkeypatch):
    """Disable the v10 quality gate by default in the test suite.

    Most existing integration tests seed records with deliberately synthetic
    content ("A" * 400, "kubernetes operator pattern…") that the gate would
    correctly reject as low-signal noise. Tests that exercise the gate
    explicitly override `MEMORY_QUALITY_GATE_ENABLED=true` themselves.
    """
    monkeypatch.setenv("MEMORY_QUALITY_GATE_ENABLED", "false")
    yield


@pytest.fixture(autouse=True)
def _disable_contradiction_detector_in_tests(monkeypatch):
    """Disable v10 auto-contradiction detection by default in the test
    suite. The detector spawns an LLM round-trip per same-type candidate
    in the same project — fine in production, but we don't want fixture
    saves in different tests cross-comparing each other or accidentally
    talking to a half-configured provider. The dedicated detector tests
    drive `detect_contradictions` directly; they don't need the hot-path
    integration to fire."""
    monkeypatch.setenv("MEMORY_CONTRADICTION_DETECT_ENABLED", "false")
    yield


@pytest.fixture(autouse=True)
def _disable_async_enrichment_in_tests(monkeypatch):
    """v11.0: fast mode default flips MEMORY_ASYNC_ENRICHMENT=true. The
    background worker thread it spawns races with `db.close()` during
    pytest teardown and produces 'Cannot operate on a closed database'
    errors plus occasional 'sqlite3 not an error' on subsequent fixture
    setups. Tests that need the worker (test_async_enrichment, the v11
    eval suite) explicitly opt in via monkeypatch.setenv inside the
    test."""
    monkeypatch.setenv("MEMORY_ASYNC_ENRICHMENT", "false")
    yield


@pytest.fixture
def db():
    """In-memory SQLite database with all v5 tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Apply v5 migration
    migration = Path(__file__).parent.parent / "migrations" / "001_v5_schema.sql"
    conn.executescript(migration.read_text())

    # Migration 026 — case-insensitive name normalization for graph_nodes.
    # Tests that touch graph_store rely on name_norm being present so
    # add_node can do its UPSERT lookup.
    m026 = Path(__file__).parent.parent / "migrations" / "026_graph_nodes_dedup.sql"
    if m026.exists():
        conn.executescript(m026.read_text())

    # Add base tables (from main server, not in v5 migration)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
            project TEXT DEFAULT 'general', status TEXT DEFAULT 'open',
            summary TEXT, log_count INTEGER DEFAULT 0, branch TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, type TEXT, content TEXT, context TEXT DEFAULT '',
            project TEXT DEFAULT 'general', tags TEXT DEFAULT '[]',
            status TEXT DEFAULT 'active', confidence REAL DEFAULT 1.0,
            created_at TEXT, updated_at TEXT, recall_count INTEGER DEFAULT 0,
            last_recalled TEXT, last_confirmed TEXT, superseded_by INTEGER,
            source TEXT DEFAULT 'explicit', branch TEXT DEFAULT ''
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
            content, context, tags, content='knowledge', content_rowid='id'
        );
        CREATE TRIGGER IF NOT EXISTS k_fts_i AFTER INSERT ON knowledge BEGIN
            INSERT INTO knowledge_fts(rowid, content, context, tags)
            VALUES (new.id, new.content, new.context, new.tags);
        END;
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY, session_id TEXT, content TEXT, context TEXT,
            category TEXT, scope TEXT DEFAULT 'global', priority INTEGER DEFAULT 5,
            source_insight_id INTEGER, project TEXT DEFAULT 'general',
            tags TEXT DEFAULT '[]', status TEXT DEFAULT 'active',
            fire_count INTEGER DEFAULT 0, success_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0, success_rate REAL DEFAULT 0.0,
            last_fired TEXT, created_at TEXT, updated_at TEXT
        );
    """)

    yield conn
    conn.close()


@pytest.fixture
def graph_store(db):
    from graph.store import GraphStore
    return GraphStore(db)


@pytest.fixture
def graph_query(graph_store):
    from graph.query import GraphQuery
    return GraphQuery(graph_store)


@pytest.fixture
def episode_store(db):
    from memory_systems.episode_store import EpisodeStore
    return EpisodeStore(db)


@pytest.fixture
def skill_store(db):
    from memory_systems.skill_store import SkillStore
    return SkillStore(db)


@pytest.fixture
def self_model(db):
    from memory_systems.self_model import SelfModel
    return SelfModel(db)


@pytest.fixture
def activation(db):
    from associative.activation import SpreadingActivation
    return SpreadingActivation(db)


@pytest.fixture
def populated_graph(db, graph_store):
    """Graph with test nodes and edges for traversal tests."""
    auth = graph_store.add_node("concept", "authentication", content="Auth concept")
    billing = graph_store.add_node("concept", "billing", content="Billing")
    saas = graph_store.add_node("concept", "saas", content="SaaS platform")
    go = graph_store.add_node("technology", "go", content="Go language")
    jwt = graph_store.add_node("concept", "jwt", content="JSON Web Tokens")
    webhook = graph_store.add_node("concept", "webhook", content="Webhook handling")

    graph_store.add_edge(saas, auth, "requires", weight=0.9)
    graph_store.add_edge(saas, billing, "requires", weight=0.8)
    graph_store.add_edge(auth, jwt, "uses", weight=0.9)
    graph_store.add_edge(auth, go, "uses", weight=0.7)
    graph_store.add_edge(billing, webhook, "integrates_with", weight=0.6)
    graph_store.add_edge(webhook, go, "uses", weight=0.5)

    return {
        "auth": auth, "billing": billing, "saas": saas,
        "go": go, "jwt": jwt, "webhook": webhook,
    }
