"""Tests for multi_repr_search tier."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def mrs_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    root = Path(__file__).parent.parent
    conn.executescript((root / "migrations" / "001_v5_schema.sql").read_text())
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT, project TEXT DEFAULT 'general',
            status TEXT DEFAULT 'active', created_at TEXT
        );
        """
    )
    conn.executescript((root / "migrations" / "002_multi_representation.sql").read_text())
    yield conn
    conn.close()


def _det_emb(text: str, dim: int = 8) -> list[float]:
    h = hashlib.md5(text.encode()).digest()
    return [b / 255.0 for b in h[:dim]]


def _seed(db, text: str, project: str = "demo") -> int:
    return db.execute(
        "INSERT INTO knowledge (content, project, status, created_at) "
        "VALUES (?, ?, 'active', '2026-04-14T00:00:00Z')",
        (text, project),
    ).lastrowid


def _add_repr(db, kid: int, repr_name: str, text: str):
    from multi_repr_store import MultiReprStore
    MultiReprStore(db).upsert(kid, repr_name, text, _det_emb(text), "fake")


def test_has_representations_detects_empty(mrs_db):
    from multi_repr_search import has_representations

    assert has_representations(mrs_db) is False


def test_has_representations_detects_populated(mrs_db):
    from multi_repr_search import has_representations

    kid = _seed(mrs_db, "doc")
    _add_repr(mrs_db, kid, "summary", "short")
    assert has_representations(mrs_db) is True


def test_search_returns_empty_on_empty_table(mrs_db):
    from multi_repr_search import search

    assert search(mrs_db, _det_emb("query")) == []


def test_search_ranks_best_match(mrs_db):
    """Query closely matches summary of kid1; kid1 should rank first."""
    from multi_repr_search import search

    k1 = _seed(mrs_db, "k1 full content")
    k2 = _seed(mrs_db, "k2 full content")
    # k1 summary matches query; k2 summary does not
    _add_repr(mrs_db, k1, "summary", "quantum computing basics")
    _add_repr(mrs_db, k2, "summary", "cooking recipes")
    # Also add keywords for realism
    _add_repr(mrs_db, k1, "keywords", "quantum, qubit")
    _add_repr(mrs_db, k2, "keywords", "pasta, tomato")

    # Query embedding = same as k1's summary → perfect match
    q_emb = _det_emb("quantum computing basics")
    result = search(mrs_db, q_emb, top_n=5)
    ids = [r[0] for r in result]
    assert ids[0] == k1


def test_search_filters_by_project(mrs_db):
    from multi_repr_search import search

    k_demo = _seed(mrs_db, "demo record", project="demo")
    k_other = _seed(mrs_db, "other record", project="other")
    _add_repr(mrs_db, k_demo, "summary", "shared phrase alpha")
    _add_repr(mrs_db, k_other, "summary", "shared phrase alpha")

    q_emb = _det_emb("shared phrase alpha")
    result = search(mrs_db, q_emb, project="demo", top_n=5)
    ids = [r[0] for r in result]
    assert k_demo in ids
    assert k_other not in ids


def test_search_skips_archived(mrs_db):
    from multi_repr_search import search

    active = _seed(mrs_db, "active")
    archived = _seed(mrs_db, "archived")
    _add_repr(mrs_db, active, "summary", "target phrase")
    _add_repr(mrs_db, archived, "summary", "target phrase")

    mrs_db.execute("UPDATE knowledge SET status='archived' WHERE id=?", (archived,))
    mrs_db.commit()

    q_emb = _det_emb("target phrase")
    ids = [r[0] for r in search(mrs_db, q_emb, top_n=5)]
    assert active in ids
    assert archived not in ids


def test_search_tolerates_dim_mismatch(mrs_db):
    """If a representation has different embed_dim than query, skip it safely."""
    from multi_repr_search import search
    from multi_repr_store import MultiReprStore

    kid = _seed(mrs_db, "doc")
    # 16-dim instead of default 8
    MultiReprStore(mrs_db).upsert(
        kid, "summary", "foo", _det_emb("foo", dim=16), "fake16"
    )

    # Query in 8-dim
    ids = [r[0] for r in search(mrs_db, _det_emb("foo", dim=8), top_n=5)]
    # No crash; kid skipped because of dim mismatch
    assert kid not in ids


def test_every_representation_row_of_the_scope_is_scored(mrs_db):
    """Until 14.8.0 only the first 100 rows in table order were scored; the 150th was invisible."""
    from multi_repr_search import search

    target = None
    for index in range(150):
        kid = _seed(mrs_db, f"filler record {index}")
        _add_repr(mrs_db, kid, "summary", f"filler summary {index}")
        target = kid
    query = _det_emb("filler summary 149")
    ranked = search(mrs_db, query, project="demo", top_n=5)
    assert ranked and ranked[0][0] == target
    assert ranked[0][1] > ranked[-1][1]  # fused RRF score, best first


def test_rows_of_another_embedding_dimension_are_ignored(mrs_db):
    from multi_repr_search import search
    from multi_repr_store import MultiReprStore

    kid = _seed(mrs_db, "wide")
    MultiReprStore(mrs_db).upsert(kid, "summary", "wide", _det_emb("wide", dim=16), "other-model")
    assert search(mrs_db, _det_emb("wide"), project="demo", top_n=5) == []
