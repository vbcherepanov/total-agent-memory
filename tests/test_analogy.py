"""Tests for src/analogy.py — v7.0 Phase H."""

import json
import sqlite3

import pytest

from analogy import AnalogyEngine


@pytest.fixture
def adb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, type TEXT, content TEXT, context TEXT DEFAULT '',
            project TEXT DEFAULT 'general', tags TEXT DEFAULT '[]',
            status TEXT DEFAULT 'active', confidence REAL DEFAULT 0.9,
            created_at TEXT DEFAULT '2026-04-14T10:00:00Z',
            recall_count INTEGER DEFAULT 0
        );
    """)
    yield conn
    conn.close()


@pytest.fixture
def ae(adb):
    return AnalogyEngine(adb)


def _add(db, *, type, content, tags=None, project="p1"):
    db.execute(
        """INSERT INTO knowledge (type, content, tags, project)
           VALUES (?, ?, ?, ?)""",
        (type, content, json.dumps(tags or []), project),
    )
    db.commit()


# ──────────────────────────────────────────────
# Tokenization + ranking
# ──────────────────────────────────────────────

def test_analogy_ranks_by_feature_overlap(ae, adb):
    _add(adb, type="solution",
         content="deduplicate database records using content hash",
         tags=["dedup", "database"])
    _add(adb, type="solution",
         content="completely unrelated topic about cooking pasta",
         tags=["cooking"])
    results = ae.find_analogies(
        text="how to deduplicate records with hash in database",
    )
    assert len(results) >= 1
    assert "deduplicate" in results[0]["content"]
    assert results[0]["analogy_score"] > 0


def test_analogy_excludes_own_project(ae, adb):
    _add(adb, type="solution", content="caching strategy redis cluster",
         tags=["cache", "redis"], project="mine")
    _add(adb, type="solution", content="caching strategy memcached cluster",
         tags=["cache", "memcached"], project="other")

    results = ae.find_analogies(
        text="caching strategy cluster",
        exclude_project="mine",
    )
    projects = {r["project"] for r in results}
    assert "mine" not in projects
    assert "other" in projects


def test_analogy_returns_shared_features(ae, adb):
    _add(adb, type="solution",
         content="binary quantization speeds up vector search",
         tags=["binary", "quantization"])
    results = ae.find_analogies(
        text="vector search quantization binary pattern",
    )
    assert len(results) >= 1
    # Should report some overlap — tokens come from both content and tags
    assert len(results[0]["shared_features"]) >= 1


def test_analogy_respects_type_filter(ae, adb):
    _add(adb, type="solution", content="alpha beta gamma pattern match")
    _add(adb, type="fact", content="alpha beta gamma pattern match")
    results = ae.find_analogies(
        text="alpha beta gamma pattern match",
        only_types=("solution",),
    )
    types = {r["type"] for r in results}
    assert "solution" in types
    assert "fact" not in types


def test_analogy_min_score_filters_noise(ae, adb):
    _add(adb, type="solution", content="unrelated content about something")
    results = ae.find_analogies(
        text="completely different words nothing overlap",
        min_score=0.2,
    )
    assert results == []


def test_analogy_limit_respected(ae, adb):
    for i in range(5):
        _add(adb, type="solution",
             content=f"caching strategy redis cluster number {i}")
    results = ae.find_analogies(text="caching redis", limit=2)
    assert len(results) <= 2


# ──────────────────────────────────────────────
# transfer_lessons wrapper
# ──────────────────────────────────────────────

def test_transfer_lessons_excludes_target_project(ae, adb):
    _add(adb, type="solution", content="rate limit leaky bucket", project="target")
    _add(adb, type="lesson", content="rate limit leaky bucket strategy", project="other")
    out = ae.transfer_lessons(target_project="target", text="rate limit leaky bucket")
    assert out["count"] == 1
    assert out["analogies"][0]["project"] == "other"


def test_transfer_lessons_empty_when_no_matches(ae):
    out = ae.transfer_lessons(target_project="x", text="xyzzy frob nitz")
    assert out["count"] == 0
