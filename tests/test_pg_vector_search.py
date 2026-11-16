"""memory_core.pg_vector_search against the SQLite VectorSearch it replaces on PostgreSQL."""

from __future__ import annotations

import sqlite3
import struct

import numpy as np
import pytest

from memory_core.pg_vector_search import (
    HNSW_EF_SEARCH_ENV,
    VECTOR_INDEX_ENV,
    PgVectorSearch,
    VectorIndex,
    configured_ef_search,
    configured_index,
    vector_literal,
    vector_literal_from_blob,
)
from memory_core.vector_search import VectorScope, VectorSearch

VECTOR_SEED = 11
DIMENSION = 16
OTHER_DIMENSION = 8
RECORDS = 400
QUERIES = 25
LIMIT = 10
SCORE_TOLERANCE = 1e-5
# SQLite ranks everything exactly up to this pool size.
EXACT_LIMIT = 1_000_000
MIN_HNSW_RECALL = 0.9
PROJECTS = ("alpha", "beta")
MODELS = ("model-a", "model-b")
SPACES = ("text", "code", None)
KINDS = ("fact", "decision")
BRANCHES = ("", "main", "dev")


# ─── pure helpers ───────────────────────────────────────────────────────────

def test_vector_literal_round_trips_float32_exactly():
    rng = np.random.default_rng(VECTOR_SEED)
    values = rng.standard_normal(64).astype(np.float32)
    literal = vector_literal(values)
    parsed = np.asarray([float(item) for item in literal.strip("[]").split(",")], dtype=np.float32)
    assert np.array_equal(parsed, values)
    blob = struct.pack(f"{len(values)}f", *values.tolist())
    assert vector_literal_from_blob(blob) == literal


@pytest.mark.parametrize("values", [[], [float("nan")], [float("inf")], [[1.0, 2.0]]])
def test_vector_literal_rejects_invalid_vectors(values):
    with pytest.raises(ValueError):
        vector_literal(values)


@pytest.mark.parametrize("blob", [b"", b"\x00\x00\x00"])
def test_vector_literal_from_blob_rejects_bad_blobs(blob):
    with pytest.raises(ValueError):
        vector_literal_from_blob(blob)


def test_index_and_ef_search_configuration():
    assert configured_index({}) is VectorIndex.EXACT
    assert configured_index({VECTOR_INDEX_ENV: " HNSW "}) is VectorIndex.HNSW
    with pytest.raises(ValueError, match=VECTOR_INDEX_ENV):
        configured_index({VECTOR_INDEX_ENV: "ivfflat"})
    assert configured_ef_search({HNSW_EF_SEARCH_ENV: "64"}) == 64
    for raw in ("0", "-3", "many"):
        with pytest.raises(ValueError, match=HNSW_EF_SEARCH_ENV):
            configured_ef_search({HNSW_EF_SEARCH_ENV: raw})


# ─── PostgreSQL ─────────────────────────────────────────────────────────────

def _records() -> list[dict]:
    rng = np.random.default_rng(VECTOR_SEED)
    records = []
    for index in range(RECORDS):
        dimension = OTHER_DIMENSION if index % 10 == 9 else DIMENSION
        records.append({
            "id": index + 1,
            "project": PROJECTS[index % 2],
            "type": KINDS[index % 3 % 2],
            "branch": BRANCHES[index % 3],
            "status": "archived" if index % 17 == 0 else "active",
            "model": MODELS[index % 5 % 2],
            "space": SPACES[index % 7 % 3],
            "vector": rng.standard_normal(dimension).astype(np.float32),
        })
    return records


def _insert(connection, records: list[dict], *, postgres: bool) -> None:
    for record in records:
        connection.execute(
            "INSERT INTO knowledge (id, session_id, type, content, project, branch, status, created_at) "
            "VALUES (?, 's', ?, 'c', ?, ?, ?, '2026-01-01T00:00:00Z')",
            (record["id"], record["type"], record["project"], record["branch"], record["status"]))
        vector = record["vector"]
        blob = struct.pack(f"{len(vector)}f", *vector.tolist())
        binary = np.packbits(vector > 0).tobytes()
        columns = ("knowledge_id, binary_vector, float32_vector, embed_model, embed_dim, created_at, "
                   "embedding_provider, embedding_space")
        values = (record["id"], binary, blob, record["model"], len(vector), "2026-01-01T00:00:00Z", "fastembed",
                  record["space"])
        if postgres:
            connection.execute(f"INSERT INTO embeddings ({columns}, embedding) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CAST(? AS vector))", (*values, vector_literal(vector)))
        else:
            connection.execute(f"INSERT INTO embeddings ({columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values)
    connection.commit()


@pytest.fixture(scope="module")
def engines(tmp_path_factory, pg_server):
    """(SQLite Store connection, PostgreSQL PgConnection) with the same records and embeddings."""
    import server
    from tam_db import pg_connection
    from tests.pg_store_support import provision_workspace
    from tests.pg_support import fresh_database

    root = tmp_path_factory.mktemp("vector-parity")
    previous = server.MEMORY_DIR
    server.MEMORY_DIR = root
    try:
        store = server.Store()
    finally:
        server.MEMORY_DIR = previous
    records = _records()
    with fresh_database(pg_server) as database:
        pg = pg_connection.connect(provision_workspace(database.url, "vector-parity"))
        pg.row_factory = sqlite3.Row
        try:
            _insert(store.db, records, postgres=False)
            _insert(pg, records, postgres=True)
            yield store.db, pg
        finally:
            pg.close()
            store.db.close()


def _scopes() -> list[VectorScope]:
    return [
        VectorScope(dimension=DIMENSION),
        VectorScope(dimension=DIMENSION, project="alpha"),
        VectorScope(dimension=DIMENSION, project="beta", model="model-a"),
        VectorScope(dimension=DIMENSION, kind="decision"),
        VectorScope(dimension=DIMENSION, branch="main"),
        VectorScope(dimension=DIMENSION, spaces=("code",)),
        VectorScope(dimension=DIMENSION, spaces=("text",), project="alpha", branch="dev"),
        VectorScope(dimension=OTHER_DIMENSION),
    ]


def _queries(dimension: int) -> list[list[float]]:
    rng = np.random.default_rng(VECTOR_SEED + dimension)
    return [rng.standard_normal(dimension).astype(np.float32).tolist() for _ in range(QUERIES)]


@pytest.mark.postgres
def test_exact_search_ranks_like_sqlite(engines):
    db, pg = engines
    sqlite_search = VectorSearch(db)
    pg_search = PgVectorSearch(pg, index=VectorIndex.EXACT)
    for scope in _scopes():
        for query in _queries(scope.dimension):
            expected = sqlite_search.search(query, scope, candidates=LIMIT, limit=LIMIT, exact_limit=EXACT_LIMIT)
            actual = pg_search.search(query, scope, candidates=LIMIT, limit=LIMIT)
            assert [row[0] for row in actual] == [row[0] for row in expected], scope
            for (_, score), (_, reference) in zip(actual, expected, strict=True):
                assert score == pytest.approx(reference, abs=SCORE_TOLERANCE)


@pytest.mark.postgres
def test_exact_search_is_at_least_as_good_as_the_binary_prefilter(engines):
    db, pg = engines
    sqlite_search = VectorSearch(db)
    pg_search = PgVectorSearch(pg, index=VectorIndex.EXACT)
    scope = VectorScope(dimension=DIMENSION)
    for query in _queries(DIMENSION):
        prefiltered = sqlite_search.search(query, scope, candidates=LIMIT, limit=LIMIT, exact_limit=0)
        exact = pg_search.search(query, scope, candidates=LIMIT, limit=LIMIT)
        for (_, found), (_, best) in zip(prefiltered, exact, strict=True):
            assert best >= found - SCORE_TOLERANCE


@pytest.mark.postgres
def test_groups_match_sqlite_and_follow_revisions(engines):
    db, pg = engines
    sqlite_search = VectorSearch(db)
    pg_search = PgVectorSearch(pg)
    for project in (None, *PROJECTS, "missing"):
        assert pg_search.groups(project) == tuple(sorted(sqlite_search.groups(project)))
    assert pg_search.groups("alpha") is pg_search.groups("alpha")
    vector = np.ones(DIMENSION, dtype=np.float32)
    extra = [{"id": RECORDS + 1, "project": "alpha", "type": "fact", "branch": "", "status": "active",
              "model": "model-new", "space": "log", "vector": vector}]
    _insert(pg, extra, postgres=True)
    assert ("log", "model-new", DIMENSION) in pg_search.groups("alpha")
    pg.execute("DELETE FROM knowledge WHERE id = ?", (RECORDS + 1,))
    pg.execute("DELETE FROM embeddings WHERE knowledge_id = ?", (RECORDS + 1,))
    pg.commit()
    assert ("log", "model-new", DIMENSION) not in pg_search.groups("alpha")
    pg_search.clear()
    assert pg_search.group_cache == {}


@pytest.mark.postgres
def test_groups_are_not_cached_inside_a_transaction(engines):
    _, pg = engines
    pg_search = PgVectorSearch(pg)
    pg.execute("UPDATE knowledge SET recall_count = recall_count + 1 WHERE id = 1")
    assert pg.in_transaction
    pg_search.groups("alpha")
    assert pg_search.group_cache == {}
    pg.rollback()
    pg_search.groups("alpha")
    assert "alpha" in pg_search.group_cache


@pytest.mark.postgres
def test_search_validates_like_sqlite(engines):
    _, pg = engines
    pg_search = PgVectorSearch(pg)
    scope = VectorScope(dimension=DIMENSION)
    query = [1.0] * DIMENSION
    for kwargs in ({"candidates": 0, "limit": 1}, {"candidates": 1, "limit": 0},
                   {"candidates": 1, "limit": 1, "exact_limit": -1}):
        with pytest.raises(ValueError):
            pg_search.search(query, scope, **kwargs)
    for bad in ([1.0] * (DIMENSION - 1), [float("nan")] * DIMENSION):
        with pytest.raises(ValueError):
            pg_search.search(bad, scope, candidates=1, limit=1)
    with pytest.raises(ValueError):
        PgVectorSearch(pg, max_cache_bytes=-1)
    with pytest.raises(ValueError):
        PgVectorSearch(pg, ef_search=0)


@pytest.mark.postgres
def test_rows_without_a_pgvector_copy_are_skipped(engines):
    _, pg = engines
    pg.execute("INSERT INTO knowledge (id, session_id, type, content, project, created_at) "
               "VALUES (?, 's', 'fact', 'c', 'alpha', '2026-01-01T00:00:00Z')", (RECORDS + 2,))
    vector = np.ones(DIMENSION, dtype=np.float32)
    pg.execute("INSERT INTO embeddings (knowledge_id, binary_vector, float32_vector, embed_model, embed_dim, "
               "created_at) VALUES (?, ?, ?, 'model-a', ?, '2026-01-01T00:00:00Z')",
               (RECORDS + 2, np.packbits(vector > 0).tobytes(), vector.tobytes(), DIMENSION))
    pg.commit()
    try:
        found = PgVectorSearch(pg).search(vector.tolist(), VectorScope(dimension=DIMENSION),
                                          candidates=LIMIT, limit=RECORDS)
        assert RECORDS + 2 not in {row[0] for row in found}
    finally:
        pg.execute("DELETE FROM embeddings WHERE knowledge_id = ?", (RECORDS + 2,))
        pg.execute("DELETE FROM knowledge WHERE id = ?", (RECORDS + 2,))
        pg.commit()


@pytest.mark.postgres
def test_hnsw_search_uses_prepared_index_and_finds_the_neighbours(engines):
    from tam_db import pg_schema

    db, pg = engines
    pg_schema.ensure_hnsw_index(pg, DIMENSION)
    sqlite_search = VectorSearch(db)
    pg_search = PgVectorSearch(pg, index=VectorIndex.HNSW, ef_search=200)
    scope = VectorScope(dimension=DIMENSION, project="alpha")
    recalls = []
    for query in _queries(DIMENSION):
        expected = sqlite_search.search(query, scope, candidates=LIMIT, limit=LIMIT, exact_limit=EXACT_LIMIT)
        actual = pg_search.search(query, scope, candidates=LIMIT, limit=LIMIT)
        assert [row[1] for row in actual] == sorted((row[1] for row in actual), reverse=True)
        recalls.append(len({row[0] for row in actual} & {row[0] for row in expected}) / len(expected))
    assert sum(recalls) / len(recalls) >= MIN_HNSW_RECALL
    index = pg.execute_native("SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                              "AND indexname = %s", (pg_schema.hnsw_index_name(DIMENSION),)).fetchone()
    assert index is not None and "hnsw" in index[0] and f"embed_dim = {DIMENSION}" in index[0]
    pg_schema.ensure_hnsw_index(pg, DIMENSION)
    with pytest.raises(ValueError):
        pg_schema.ensure_hnsw_index(pg, 0)


@pytest.mark.postgres
def test_unprepared_hnsw_falls_back_without_ddl(engines, monkeypatch):
    from tam_db import pg_schema

    _, pg = engines
    scope = VectorScope(dimension=OTHER_DIMENSION, project='alpha')
    query = next(iter(_queries(OTHER_DIMENSION)))
    exact = PgVectorSearch(pg, index=VectorIndex.EXACT).search(query, scope, candidates=LIMIT, limit=LIMIT)

    def forbidden(*args, **kwargs):
        pytest.fail('A search must not provision indexes')

    monkeypatch.setattr(pg_schema, 'ensure_hnsw_index', forbidden)
    result = PgVectorSearch(pg, index=VectorIndex.HNSW).search(query, scope, candidates=LIMIT, limit=LIMIT)
    assert result == exact
    assert pg_schema.hnsw_index_ready(pg, OTHER_DIMENSION) is False


@pytest.mark.postgres
def test_provisioning_repairs_failed_concurrent_index(engines):
    import psycopg
    from tam_db import pg_schema

    _, pg = engines
    kid = pg.execute("SELECT knowledge_id FROM embeddings WHERE embed_dim=? LIMIT 1",
                     (DIMENSION,)).fetchone()[0]
    pg.execute("UPDATE embeddings SET embed_dim=? WHERE knowledge_id=?", (OTHER_DIMENSION, kid))
    pg.commit()
    try:
        with pytest.raises(psycopg.Error):
            pg_schema.ensure_hnsw_index(pg, OTHER_DIMENSION)
        assert pg_schema._hnsw_index_state(pg, OTHER_DIMENSION) == 'invalid'
        assert not pg_schema.hnsw_index_ready(pg, OTHER_DIMENSION)
    finally:
        pg.execute("UPDATE embeddings SET embed_dim=? WHERE knowledge_id=?", (DIMENSION, kid))
        pg.commit()
    pg_schema.ensure_hnsw_index(pg, OTHER_DIMENSION)
    assert pg_schema.hnsw_index_ready(pg, OTHER_DIMENSION)


@pytest.mark.postgres
def test_provisioning_refuses_an_existing_incompatible_index(engines):
    from psycopg import sql
    from tam_db import pg_schema

    _, pg = engines
    unused_dimension = DIMENSION + OTHER_DIMENSION
    name = pg_schema.hnsw_index_name(unused_dimension)
    native = pg_schema.native_connection(pg)
    native.execute(sql.SQL('CREATE INDEX {} ON embeddings(knowledge_id)').format(sql.Identifier(name)))
    try:
        with pytest.raises(pg_schema.WorkspaceSchemaError, match='incompatible definition'):
            pg_schema.ensure_hnsw_index(pg, unused_dimension)
        index = native.execute('SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() '
                               'AND indexname=%s', (name,)).fetchone()
        assert 'btree' in index[0]
    finally:
        native.execute(sql.SQL('DROP INDEX {}').format(sql.Identifier(name)))
