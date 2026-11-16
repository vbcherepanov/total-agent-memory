"""Vector search over the PostgreSQL workspace schema (pgvector).

Same public surface as memory_core.vector_search.VectorSearch (``groups``,
``search``, ``clear``) so Store uses either unchanged. SQLite keeps packed float32
BLOBs and ranks them in process with a binary prefilter; PostgreSQL stores each
embedding once more in ``embeddings.embedding`` (pgvector, written next to the BLOBs
by Store._upsert_embedding) and ranks inside the database:

* ``exact`` (default): an exact cosine scan of the rows in scope. Same similarity
  as SQLite's exact pass, and never worse than its binary-prefiltered pass.
* ``hnsw``: approximate search through a pre-provisioned partial HNSW index per dimension
  (prepared by tam_db.pg_schema.ensure_hnsw_index) with pgvector's
  iterative scan, for workspaces too large for an exact scan.

Scores are cosine similarities, ties broken by the lower knowledge id, like SQLite.
"""

from __future__ import annotations

import math
import os
import threading
from collections import OrderedDict
from collections.abc import Sequence
from enum import StrEnum

import numpy as np

from memory_core.telemetry import counters, op_timer
from memory_core.vector_search import (
    DEFAULT_VECTOR_CACHE_BYTES,
    MAX_VECTOR_CACHE_ENTRIES,
    VectorScope,
)

VECTOR_INDEX_ENV = "TAM_TEAM_PG_VECTOR_INDEX"
HNSW_EF_SEARCH_ENV = "TAM_TEAM_PG_HNSW_EF_SEARCH"
DEFAULT_HNSW_EF_SEARCH = 100
# pgvector's iterative scan keeps visiting the graph until enough rows pass the
# scope filter; relaxed_order returns them approximately ordered and search()
# re-sorts them.
HNSW_ITERATIVE_SCAN = "relaxed_order"


class VectorIndex(StrEnum):
    EXACT = "exact"
    HNSW = "hnsw"


def vector_literal(values: Sequence[float] | np.ndarray) -> str:
    """pgvector text for ``values``, exact for float32: each element is the shortest
    decimal that parses back to the same float32, the precision the BLOB columns keep."""
    array = np.asarray(values, dtype=np.float32)
    if array.ndim != 1 or not array.size or not np.isfinite(array).all():
        raise ValueError("A vector must be a non-empty, finite, one-dimensional sequence")
    return "[" + ",".join(repr(float(value)) for value in array) + "]"


def vector_literal_from_blob(blob: bytes) -> str:
    """pgvector text for a float32 BLOB as Store._float32_to_blob packs it (native order)."""
    if not blob or len(blob) % np.dtype(np.float32).itemsize:
        raise ValueError("A float32 vector BLOB must be a non-empty multiple of 4 bytes")
    return vector_literal(np.frombuffer(blob, dtype=np.float32))


def configured_index(environ: dict[str, str] | None = None) -> VectorIndex:
    raw = (environ if environ is not None else os.environ).get(VECTOR_INDEX_ENV, "").strip().lower()
    if not raw:
        return VectorIndex.EXACT
    try:
        return VectorIndex(raw)
    except ValueError:
        raise ValueError(f"{VECTOR_INDEX_ENV} must be one of: "
                         + ", ".join(index.value for index in VectorIndex)) from None


def configured_ef_search(environ: dict[str, str] | None = None) -> int:
    raw = (environ if environ is not None else os.environ).get(HNSW_EF_SEARCH_ENV, "").strip()
    if not raw:
        return DEFAULT_HNSW_EF_SEARCH
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{HNSW_EF_SEARCH_ENV} must be a positive integer")
    return int(raw)


class PgVectorSearch:
    def __init__(self, db, max_cache_bytes: int = DEFAULT_VECTOR_CACHE_BYTES, *,
                 index: VectorIndex | None = None, ef_search: int | None = None):
        if type(max_cache_bytes) is not int or max_cache_bytes < 0:
            raise ValueError("Vector cache budget must be a non-negative integer")
        self.db = db
        # Vectors stay in PostgreSQL; the budget only decides whether group lists are cached.
        self.max_cache_bytes = max_cache_bytes
        self.index = index if index is not None else configured_index()
        self.ef_search = ef_search if ef_search is not None else configured_ef_search()
        if type(self.ef_search) is not int or self.ef_search < 1:
            raise ValueError("HNSW ef_search must be a positive integer")
        self.group_cache: OrderedDict[str | None, tuple[tuple[str, str, int], ...]] = OrderedDict()
        self.revision: int | None = None
        self.hnsw_session = False
        self.lock = threading.RLock()

    def clear(self) -> None:
        with self.lock:
            self.group_cache.clear()
            self.revision = None

    def _revision(self) -> int:
        return self.db.execute("SELECT revision FROM vector_index_revision WHERE singleton=1").fetchone()[0]

    def groups(self, project: str | None) -> tuple[tuple[str, str, int], ...]:
        with self.lock, op_timer("vector_groups_ms"):
            revision = self._revision()
            if self.db.in_transaction or revision != self.revision:
                self.group_cache.clear()
                self.revision = None if self.db.in_transaction else revision
            if project in self.group_cache:
                self.group_cache.move_to_end(project)
                counters.bump("vector_groups_cache_hits")
                return self.group_cache[project]
            params = (project,) if project is not None else ()
            rows = self.db.execute(
                "SELECT DISTINCT COALESCE(e.embedding_space,'text'),e.embed_model,e.embed_dim "
                "FROM embeddings e JOIN knowledge k ON k.id=e.knowledge_id WHERE k.status='active'"
                + (" AND k.project=?" if project is not None else "")
                + " ORDER BY 1,2,3", params,
            ).fetchall()
            result = tuple((row[0], row[1], int(row[2])) for row in rows)
            if self.max_cache_bytes and self.revision is not None:
                while len(self.group_cache) >= MAX_VECTOR_CACHE_ENTRIES:
                    self.group_cache.popitem(last=False)
                self.group_cache[project] = result
            return result

    def search(
        self, query: list[float], scope: VectorScope, *, candidates: int, limit: int,
        exact_limit: int = 0,
    ) -> list[tuple[int, float]]:
        """Top ``limit`` (knowledge_id, cosine) pairs in ``scope``.

        ``candidates`` and ``exact_limit`` size SQLite's in-process binary prefilter;
        they are validated the same way and otherwise unused, since the database ranks
        every row in scope.
        """
        if type(candidates) is not int or candidates < 1 or type(limit) is not int or limit < 1:
            raise ValueError("Candidate and result limits must be positive integers")
        if type(exact_limit) is not int or exact_limit < 0:
            raise ValueError("Exact scan limit must be a non-negative integer")
        q = np.asarray(query, dtype=np.float32)
        if q.ndim != 1 or q.size != scope.dimension or not q.size or not np.isfinite(q).all():
            raise ValueError("Query must be a finite vector matching its scope")
        literal = vector_literal(q)
        where, params = scope.sql()
        with self.lock, op_timer("vector_lookup_ms"):
            if self.index is VectorIndex.HNSW:
                from tam_db.pg_schema import hnsw_index_ready
                if hnsw_index_ready(self.db, scope.dimension):
                    return self._search_hnsw(literal, scope.dimension, where, params, limit)
                counters.bump('vector_hnsw_not_ready_fallbacks')
            counters.bump("vector_exact_searches")
            rows = self._native(
                "SELECT e.knowledge_id, e.embedding <=> CAST(? AS vector) AS distance "
                "FROM embeddings e JOIN knowledge k ON k.id=e.knowledge_id "
                f"WHERE {where} AND e.embedding IS NOT NULL "
                "ORDER BY distance, e.knowledge_id LIMIT ?",
                [literal, *params, limit],
            )
            counters.bump("vector_cosine_candidates", len(rows))
            return [(int(row[0]), _similarity(row[1])) for row in rows]

    def _search_hnsw(self, literal: str, dimension: int, where: str, params: list, limit: int,
                     ) -> list[tuple[int, float]]:
        if not self.hnsw_session:
            self.db.execute("SELECT set_config('hnsw.iterative_scan', ?, false), "
                            "set_config('hnsw.ef_search', ?, false)",
                            (HNSW_ITERATIVE_SCAN, str(self.ef_search))).fetchall()
            self.hnsw_session = True
        counters.bump("vector_hnsw_searches")
        # The ORDER BY expression must match the partial index expression for the
        # planner to use it; the embed_dim predicate in the scope selects that index.
        vector_type = f"vector({int(dimension)})"
        rows = self._native(
            f"SELECT e.knowledge_id, CAST(e.embedding AS {vector_type}) <=> CAST(? AS {vector_type}) AS distance "
            "FROM embeddings e JOIN knowledge k ON k.id=e.knowledge_id "
            f"WHERE {where} AND e.embedding IS NOT NULL "
            f"ORDER BY CAST(e.embedding AS {vector_type}) <=> CAST(? AS {vector_type}) LIMIT ?",
            [literal, *params, literal, limit],
        )
        counters.bump("vector_cosine_candidates", len(rows))
        ranked = [(int(row[0]), _similarity(row[1])) for row in rows]
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked


    def _native(self, sql: str, params: list) -> list:
        """Run a ranking query untranslated: the translator would add SQLite's NULL
        placement to ORDER BY, and pgvector index scans only serve a plain ascending
        distance order. ``sql`` uses ``?`` placeholders only (VectorScope's style)."""
        return self.db.execute_native(sql.replace("?", "%s"), params).fetchall()


def _similarity(distance: float | None) -> float:
    """Cosine similarity from pgvector's cosine distance; a zero vector has none (0.0)."""
    if distance is None or math.isnan(distance):
        return 0.0
    return 1.0 - float(distance)


__all__ = [
    "DEFAULT_HNSW_EF_SEARCH", "HNSW_EF_SEARCH_ENV", "VECTOR_INDEX_ENV", "PgVectorSearch", "VectorIndex",
    "configured_ef_search", "configured_index", "vector_literal", "vector_literal_from_blob",
]
