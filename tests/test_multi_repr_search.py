"""Tests for multi_repr_search tier."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from memory_core.embedding_identity import EmbedIdentity


@pytest.fixture
def mrs_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    root = Path(__file__).parent.parent
    conn.executescript((root / "migrations" / "001_v5_schema.sql").read_text(encoding="utf-8"))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT, project TEXT DEFAULT 'general',
            status TEXT DEFAULT 'active', created_at TEXT
        );
        """
    )
    conn.executescript((root / "migrations" / "002_multi_representation.sql").read_text(encoding="utf-8"))
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


@pytest.mark.parametrize('identity', [
    EmbedIdentity('foreign-model', 'declared'),
    EmbedIdentity('fake', 'another-provider'),
    EmbedIdentity('fake', 'declared', 'changed-pooling'),
    EmbedIdentity('fake', 'declared', library='changed-library'),
])
def test_same_dimension_foreign_identity_is_excluded(mrs_db, identity):
    from multi_repr_store import MultiReprStore
    from multi_repr_search import search

    kid = _seed(mrs_db, 'record')
    vector = _det_emb('query')
    MultiReprStore(mrs_db).upsert(kid, 'summary', 'query', vector, identity.model, identity=identity)
    assert search(mrs_db, vector, query_identity=EmbedIdentity('fake', 'declared')) == []


def test_unknown_legacy_identity_is_excluded(mrs_db):
    from multi_repr_search import search

    kid = _seed(mrs_db, 'record')
    _add_repr(mrs_db, kid, 'summary', 'query')
    mrs_db.execute('UPDATE knowledge_representations SET embed_identity_key=NULL')
    assert search(mrs_db, _det_emb('query'), query_identity=EmbedIdentity('fake', 'declared')) == []


def test_unknown_query_identity_is_excluded(mrs_db):
    from multi_repr_search import search

    kid = _seed(mrs_db, 'record')
    _add_repr(mrs_db, kid, 'summary', 'query')
    assert search(mrs_db, _det_emb('query')) == []


def _cache(db, budget=1024 * 1024):
    from multi_repr_search import RepresentationCache

    root = Path(__file__).resolve().parents[1]
    db.execute('CREATE TABLE embeddings(knowledge_id INTEGER PRIMARY KEY)')
    db.executescript((root / 'migrations/032_vector_index_revision.sql').read_text(encoding='utf-8'))
    db.executescript((root / 'migrations/049_representation_revision.sql').read_text(encoding='utf-8'))
    return RepresentationCache(db, max_bytes=budget)


def _cached_search(db, cache, project='demo'):
    from multi_repr_search import search_with_winners

    return search_with_winners(db, _det_emb('query'), project=project,
                               query_identity=EmbedIdentity('fake', 'declared'), cache=cache)


def test_cache_matches_uncached_and_reuses_matrices(mrs_db):
    from multi_repr_search import search_with_winners
    from memory_core.telemetry import counters

    cache = _cache(mrs_db)
    kid = _seed(mrs_db, 'record')
    _add_repr(mrs_db, kid, 'summary', 'query')
    expected = search_with_winners(mrs_db, _det_emb('query'), project='demo',
                                  query_identity=EmbedIdentity('fake', 'declared'))
    assert _cached_search(mrs_db, cache) == expected
    hits = counters.get('representation_cache_hits')
    assert _cached_search(mrs_db, cache) == expected
    assert counters.get('representation_cache_hits') > hits
    assert 0 < cache.cache_bytes <= cache.max_bytes


@pytest.mark.parametrize('mutation', [
    "UPDATE knowledge SET status='superseded'",
    "UPDATE knowledge SET project='other'",
    "DELETE FROM knowledge_representations",
    "UPDATE knowledge_representations SET embed_model='foreign'",
])
def test_cache_invalidates_on_visibility_and_vector_changes(mrs_db, mutation):
    cache = _cache(mrs_db)
    kid = _seed(mrs_db, 'record')
    _add_repr(mrs_db, kid, 'summary', 'query')
    assert _cached_search(mrs_db, cache)[0]
    mrs_db.execute(mutation)
    mrs_db.commit()
    assert _cached_search(mrs_db, cache) == ([], {})


def test_cache_does_not_publish_uncommitted_state(mrs_db):
    cache = _cache(mrs_db)
    kid = _seed(mrs_db, 'record')
    _add_repr(mrs_db, kid, 'summary', 'query')
    expected = _cached_search(mrs_db, cache)
    mrs_db.execute("UPDATE knowledge SET status='superseded'")
    assert _cached_search(mrs_db, cache) == ([], {})
    assert cache.cache_bytes == 0
    mrs_db.rollback()
    assert _cached_search(mrs_db, cache) == expected


def test_cache_respects_small_budget(mrs_db):
    cache = _cache(mrs_db, budget=1)
    kid = _seed(mrs_db, 'record')
    _add_repr(mrs_db, kid, 'summary', 'query')
    assert _cached_search(mrs_db, cache)[0]
    assert cache.cache_bytes == 0


def test_cache_invalidates_after_another_connection_commits(mrs_db, tmp_path):
    from multi_repr_search import RepresentationCache

    _cache(mrs_db)
    kid = _seed(mrs_db, 'record')
    _add_repr(mrs_db, kid, 'summary', 'query')
    path = tmp_path / 'shared.db'
    writer = sqlite3.connect(path)
    reader = sqlite3.connect(path)
    reader.row_factory = sqlite3.Row
    try:
        mrs_db.backup(writer)
        cache = RepresentationCache(reader)
        assert _cached_search(reader, cache)[0]
        writer.execute("UPDATE knowledge SET project='other'")
        writer.commit()
        assert _cached_search(reader, cache) == ([], {})
    finally:
        reader.close()
        writer.close()


def test_cache_evicts_without_cross_project_results(mrs_db):
    cache = _cache(mrs_db, budget=60)
    first = _seed(mrs_db, 'first', project='first')
    second = _seed(mrs_db, 'second', project='second')
    _add_repr(mrs_db, first, 'summary', 'query')
    _add_repr(mrs_db, second, 'summary', 'query')
    assert _cached_search(mrs_db, cache, 'first')[0][0][0] == first
    assert _cached_search(mrs_db, cache, 'second')[0][0][0] == second
    assert _cached_search(mrs_db, cache, 'first')[0][0][0] == first
    assert cache.cache_bytes <= 60


def test_has_representations_detects_populated(mrs_db):
    from multi_repr_search import has_representations

    kid = _seed(mrs_db, "doc")
    _add_repr(mrs_db, kid, "summary", "short")
    assert has_representations(mrs_db) is True


def test_search_returns_empty_on_empty_table(mrs_db):
    from multi_repr_search import search

    assert search(mrs_db, _det_emb("query"), query_identity=EmbedIdentity("fake", "declared")) == []


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
    result = search(mrs_db, q_emb, top_n=5, query_identity=EmbedIdentity("fake", "declared"))
    ids = [r[0] for r in result]
    assert ids[0] == k1


def test_search_filters_by_project(mrs_db):
    from multi_repr_search import search

    k_demo = _seed(mrs_db, "demo record", project="demo")
    k_other = _seed(mrs_db, "other record", project="other")
    _add_repr(mrs_db, k_demo, "summary", "shared phrase alpha")
    _add_repr(mrs_db, k_other, "summary", "shared phrase alpha")

    q_emb = _det_emb("shared phrase alpha")
    result = search(mrs_db, q_emb, project="demo", top_n=5, query_identity=EmbedIdentity("fake", "declared"))
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
    ids = [r[0] for r in search(mrs_db, q_emb, top_n=5, query_identity=EmbedIdentity("fake", "declared"))]
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
    ids = [r[0] for r in search(mrs_db, _det_emb("foo", dim=8), top_n=5, query_identity=EmbedIdentity("fake", "declared"))]
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
    ranked = search(mrs_db, query, project="demo", top_n=5, query_identity=EmbedIdentity("fake", "declared"))
    assert ranked and ranked[0][0] == target
    assert ranked[0][1] > ranked[-1][1]  # fused RRF score, best first


def test_rows_of_another_embedding_dimension_are_ignored(mrs_db):
    from multi_repr_search import search
    from multi_repr_store import MultiReprStore

    kid = _seed(mrs_db, "wide")
    MultiReprStore(mrs_db).upsert(kid, "summary", "wide", _det_emb("wide", dim=16), "other-model")
    assert search(mrs_db, _det_emb("wide"), project="demo", top_n=5, query_identity=EmbedIdentity("fake", "declared")) == []
