"""Tests for the v10 smart query router."""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import query_router as qr


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MEMORY_SMART_ROUTER", "auto")
    monkeypatch.delenv("MEMORY_SMART_ROUTER_FORCE", raising=False)
    yield


# ──────────────────────────────────────────────
# extract_entity_candidates
# ──────────────────────────────────────────────


def test_extract_entities_finds_capitalised_words():
    out = qr.extract_entity_candidates("Bob worked on Postgres with Alice")
    assert out == ["Bob", "Postgres", "Alice"]


def test_extract_entities_finds_snake_and_kebab():
    out = qr.extract_entity_candidates("vitamin_all uses azure-sql intensely")
    assert "vitamin_all" in out
    assert "azure-sql" in out


def test_extract_entities_drops_stopwords():
    out = qr.extract_entity_candidates("Where did the migration land?")
    assert "where" not in [t.lower() for t in out]


def test_extract_entities_dedups():
    out = qr.extract_entity_candidates("Postgres and POSTGRES and postgres again")
    # Lower-case dedup → first capitalisation wins.
    assert out.count("Postgres") <= 1


def test_extract_entities_empty_input():
    assert qr.extract_entity_candidates("") == []
    assert qr.extract_entity_candidates(None) == []  # type: ignore[arg-type]


# ──────────────────────────────────────────────
# classify_query — semantic vs relational
# ──────────────────────────────────────────────


def test_classify_relational_with_two_entities_and_connector():
    c = qr.classify_query("Who did Bob work with on the Atlas migration?")
    assert c.kind == "relational"
    assert "wh-word" in c.signals
    assert any(s.startswith("connector:") for s in c.signals)


def test_classify_relational_explicit_between():
    c = qr.classify_query("Связь между Postgres и vitamin_all?")
    assert c.kind == "relational"
    assert any(s.startswith("connector:") for s in c.signals)


def test_classify_relational_when_co_mention():
    c = qr.classify_query("Where Bob and Postgres are mentioned together")
    assert c.kind == "relational"


def test_classify_semantic_for_factual_question():
    c = qr.classify_query("how does the deploy script work")
    assert c.kind in ("semantic", "hybrid")
    assert c.kind != "relational"


def test_classify_semantic_short_entity_lookup():
    c = qr.classify_query("Postgres")
    assert c.kind == "semantic"


def test_classify_hybrid_for_mixed_signal():
    # WH-word but no connector and only one entity → mid-score → hybrid.
    c = qr.classify_query("Why is Postgres slow on the staging deploy")
    # Either hybrid or relational — mid-score depends on exact signals
    assert c.kind in ("hybrid", "relational")


def test_classify_disabled_returns_hybrid():
    import os
    saved = os.environ.get("MEMORY_SMART_ROUTER")
    os.environ["MEMORY_SMART_ROUTER"] = "false"
    try:
        c = qr.classify_query("Where Bob and Postgres connect")
        assert c.kind == "hybrid"
        assert "router disabled" in c.signals
    finally:
        if saved is not None:
            os.environ["MEMORY_SMART_ROUTER"] = saved
        else:
            os.environ.pop("MEMORY_SMART_ROUTER", None)


def test_classify_force_override_via_env(monkeypatch):
    monkeypatch.setenv("MEMORY_SMART_ROUTER_FORCE", "relational")
    c = qr.classify_query("just a single word")
    assert c.kind == "relational"
    assert any("forced-by-env" in s for s in c.signals)


def test_classify_empty_query():
    c = qr.classify_query("")
    assert c.kind == "hybrid"
    assert c.confidence == 0.0


# ──────────────────────────────────────────────
# graph_search — integration with episodic
# ──────────────────────────────────────────────


@pytest.fixture
def gdb():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, type TEXT, content TEXT,
            project TEXT, tags TEXT DEFAULT '[]',
            status TEXT DEFAULT 'active',
            created_at TEXT, last_confirmed TEXT,
            recall_count INTEGER DEFAULT 0
        );
        CREATE TABLE graph_nodes (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
            content TEXT, properties TEXT, source TEXT,
            importance REAL DEFAULT 0.5,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            mention_count INTEGER DEFAULT 1, status TEXT DEFAULT 'active'
        );
        CREATE TABLE graph_edges (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
            target_id TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
            relation_type TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            context TEXT,
            created_at TEXT NOT NULL,
            last_reinforced_at TEXT,
            reinforcement_count INTEGER DEFAULT 0,
            UNIQUE(source_id, target_id, relation_type)
        );
        CREATE TABLE knowledge_nodes (
            knowledge_id INTEGER REFERENCES knowledge(id) ON DELETE CASCADE,
            node_id TEXT REFERENCES graph_nodes(id) ON DELETE CASCADE,
            role TEXT DEFAULT 'related',
            strength REAL DEFAULT 1.0,
            PRIMARY KEY (knowledge_id, node_id)
        );
        """
    )
    yield db
    db.close()


def _seed_event(db, knowledge_id, content, entity_names, project="vito"):
    """Helper that mirrors what save_knowledge would have done."""
    import json
    import episodic as ep

    db.execute(
        "INSERT INTO knowledge (id, session_id, type, content, project, "
        "tags, created_at) VALUES (?, 's1', 'decision', ?, ?, '[]', "
        "'2026-04-27T00:00:00Z')",
        (knowledge_id, content, project),
    )
    for i, name in enumerate(entity_names):
        nid = f"k{knowledge_id}-e{i}"
        db.execute(
            "INSERT INTO graph_nodes (id, type, name, properties, "
            "first_seen_at, last_seen_at) VALUES (?, 'technology', ?, ?, "
            "'2026-04-27T00:00:00Z', '2026-04-27T00:00:00Z')",
            (nid, name, json.dumps({"project": project})),
        )
        db.execute(
            "INSERT INTO knowledge_nodes (knowledge_id, node_id, role) "
            "VALUES (?, ?, 'mentions')",
            (knowledge_id, nid),
        )
    db.commit()
    return ep.record_save_event(db, knowledge_id=knowledge_id,
                                project=project, session_id="s1")


def test_graph_search_returns_co_mentioned(gdb):
    _seed_event(gdb, 1, "Migrated Postgres replication while Bob watched",
                ["Postgres", "Bob"])
    _seed_event(gdb, 2, "Postgres uptime is good", ["Postgres"])
    _seed_event(gdb, 3, "Bob handles billing", ["Bob"])

    rows = qr.graph_search(gdb, entities=["Postgres", "Bob"], project="vito")
    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert rows[0]["_via"] == "graph_router"


def test_graph_search_single_entity_returns_all_mentions(gdb):
    _seed_event(gdb, 1, "Postgres v17 release notes", ["Postgres"])
    time.sleep(1.05)
    _seed_event(gdb, 2, "Postgres extension audit", ["Postgres"])

    rows = qr.graph_search(gdb, entities=["Postgres"], project="vito")
    assert {r["id"] for r in rows} == {1, 2}


def test_graph_search_empty_when_no_match(gdb):
    _seed_event(gdb, 1, "Random save", ["Postgres"])
    assert qr.graph_search(gdb, entities=["Memcached", "Nobody"]) == []


def test_graph_search_no_entities():
    assert qr.graph_search(None, entities=[]) == []  # type: ignore[arg-type]


def test_graph_search_intersects_entities_before_candidate_limit(gdb):
    event = _seed_event(gdb, 1, "Bob maintains Postgres", ["Postgres", "Bob"])
    assert event is not None
    gdb.execute("UPDATE graph_nodes SET last_seen_at='2000-01-01T00:00:00Z' WHERE id=?", (event.node_id,))
    for kid in range(2, 203):
        _seed_event(gdb, kid, f"Postgres standalone event {kid}", ["Postgres"])
    rows = qr.graph_search(gdb, entities=["Postgres", "Bob"], project="vito", limit=1)
    assert [row["id"] for row in rows] == [1]
