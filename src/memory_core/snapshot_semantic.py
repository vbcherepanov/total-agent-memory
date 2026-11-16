from __future__ import annotations

import hashlib
import heapq
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal, TypedDict

import numpy as np

from memory_core.retrieval import MemoryHit, SearchScope
from memory_core.telemetry import counters, op_timer

SNAPSHOT_VECTOR_BATCH = 64
SNAPSHOT_RRF_K = 60


class SnapshotEmbeddingUnavailable(RuntimeError):
    def __init__(self, space: str, message: str):
        super().__init__(message)
        self.space = space


class SnapshotEmbeddingDiagnostic(TypedDict):
    space: str
    code: Literal["embed_failed"]
    error: str


@dataclass(frozen=True)
class SnapshotVector:
    values: Sequence[float]
    model: str
    provider: str
    space: str
    identity_key: str


BatchOperation = Callable[[], list[SnapshotVector | SnapshotEmbeddingUnavailable]]
BatchPrepare = Callable[[list[MemoryHit], dict[str, SnapshotVector | None]], BatchOperation]


class SnapshotSemanticIndex:
    def __init__(self, db: sqlite3.Connection,
                 encode_record: Callable[[MemoryHit], SnapshotVector],
                 encode_query: Callable[[str, str], SnapshotVector],
                 classify_record: Callable[[MemoryHit], str],
                 prepare_batch: BatchPrepare | None = None):
        self.db = db
        self.encode_record = encode_record
        self.encode_query = encode_query
        self.classify_record = classify_record
        self.prepare_batch = prepare_batch
        self.diagnostics: list[SnapshotEmbeddingDiagnostic] = []

    def _report(self, error: SnapshotEmbeddingUnavailable) -> None:
        diagnostic: SnapshotEmbeddingDiagnostic = {"space": error.space, "code": "embed_failed", "error": str(error)}
        if diagnostic not in self.diagnostics:
            self.diagnostics.append(diagnostic)
        counters.bump("retrieval_snapshot_embedding_failures")

    def _query_vector(self, query: str, space: str,
                      vectors: dict[str, SnapshotVector | None]) -> SnapshotVector | None:
        if space not in vectors:
            try:
                vectors[space] = self.encode_query(query, space)
            except SnapshotEmbeddingUnavailable as error:
                self._report(error)
                vectors[space] = None
        return vectors[space]

    @op_timer("retrieval_snapshot_semantic_ms")
    def search(self, query: str, scope: SearchScope, limit: int) -> list[tuple[int, float]]:
        from memory_core.staged_search import run_inline
        return run_inline(self.search_steps(query, scope, limit))

    def search_steps(self, query: str, scope: SearchScope, limit: int, *, revision: Callable | None = None):
        if scope.as_of is None or limit < 1:
            raise ValueError("Snapshot semantic search requires as_of and a positive limit")
        self.diagnostics = []
        owned = not self.db.in_transaction
        try:
            result = yield from self._search(query, scope, limit, revision=revision, owned=owned)
            if owned:
                self.db.commit()
            return result
        except Exception:
            if owned:
                self.db.rollback()
            raise

    def _search(self, query: str, scope: SearchScope, limit: int, *, revision: Callable | None = None, owned: bool = True):
        predicates, params = replace(scope, spaces=None).sql()
        query_vectors: dict[str, SnapshotVector | None] = {}
        groups: dict[tuple[str, tuple[float, ...]], list[tuple[float, int]]] = {}
        spaces = {scope.spaces} if isinstance(scope.spaces, str) else set(scope.spaces or ())
        spaces = {space.strip().lower() for space in spaces}
        after = 0
        while rows := self.db.execute("SELECT k.* FROM knowledge k WHERE " + " AND ".join(predicates)
                                     + " AND k.id>? ORDER BY k.id LIMIT ?", [*params, after, SNAPSHOT_VECTOR_BATCH]).fetchall():
            after = rows[-1]["id"]
            failed: set[int] = set()
            rebuilt: set[int] = set()
            cache_rows = {}
            fingerprints = {}
            if self.prepare_batch is not None:
                missing = []
                for row in rows:
                    record = dict(row)
                    desired = self.classify_record(record)
                    if spaces and desired not in spaces and "text" not in spaces:
                        continue
                    desired_query = self._query_vector(query, desired, query_vectors)
                    fingerprint = self._fingerprint(record)
                    fingerprints[record["id"]] = fingerprint
                    cached = self.db.execute(
                        "SELECT embed_model,embedding_provider,embedding_space,float32_vector,embed_identity_key "
                        "FROM snapshot_embedding_cache WHERE knowledge_id=? AND content_hash=?",
                        (record["id"], fingerprint)).fetchone()
                    cache_rows[record["id"]] = cached
                    if cached is not None:
                        current = self._query_vector(query, cached[2], query_vectors)
                        if current is None and cached[2] == desired:
                            continue
                        if current is not None and (cached[2] == desired or desired_query is None) and (
                                cached[0], cached[1], len(cached[3]) // np.dtype(np.float32).itemsize, cached[4]) == (
                                current.model, current.provider, len(current.values), current.identity_key):
                            continue
                    missing.append(record)
                    fingerprints[record["id"]] = fingerprint
                if missing:
                    from memory_core.staged_search import InferenceCall, SearchRevisionChanged

                    # The fallback's query identity is captured before inference leaves the owner.
                    self._query_vector(query, "text", query_vectors)
                    operation = self.prepare_batch(missing, query_vectors)
                    before = revision() if revision is not None and not self.db.in_transaction else None
                    if before is not None:
                        vectors = yield InferenceCall(operation)
                        if revision() != before:
                            raise SearchRevisionChanged("Snapshot data changed during embedding inference")
                    else:
                        vectors = operation()
                    if len(vectors) != len(missing):
                        raise ValueError("Snapshot batch embedding count differs from record count")
                    for record, vector in zip(missing, vectors, strict=True):
                        if isinstance(vector, SnapshotEmbeddingUnavailable):
                            self._report(vector)
                            failed.add(record["id"])
                        else:
                            self._persist(record["id"], fingerprints[record["id"]], vector)
                            cache_rows[record["id"]] = (vector.model, vector.provider, vector.space,
                                                        np.asarray(vector.values, dtype=np.float32).tobytes(), vector.identity_key)
                            rebuilt.add(record["id"])
                    if owned:
                        self.db.commit()
            for row in rows:
                record: MemoryHit = dict(row)
                identity = int(record["id"])
                if identity in failed:
                    continue
                desired_space = self.classify_record(record)
                if spaces and desired_space not in spaces and "text" not in spaces:
                    continue
                desired_query = self._query_vector(query, desired_space, query_vectors)
                fingerprint = fingerprints[identity] if identity in fingerprints else self._fingerprint(record)
                cached = cache_rows[identity] if identity in cache_rows else self.db.execute(
                    "SELECT embed_model,embedding_provider,embedding_space,float32_vector,embed_identity_key "
                    "FROM snapshot_embedding_cache WHERE knowledge_id=? AND content_hash=?",
                    (identity, fingerprint),
                ).fetchone()
                vector = None
                if cached is not None:
                    candidate = SnapshotVector(np.frombuffer(bytes(cached[3]), dtype=np.float32),
                                               cached[0], cached[1], cached[2], cached[4])
                    current = self._query_vector(query, candidate.space, query_vectors)
                    if current is None and candidate.space == desired_space:
                        continue
                    if current is not None and (candidate.space == desired_space or desired_query is None) and (
                            candidate.model, candidate.provider, len(candidate.values), candidate.identity_key) == (
                            current.model, current.provider, len(current.values), current.identity_key):
                        vector = candidate
                        if identity not in rebuilt:
                            counters.bump("retrieval_snapshot_vector_cache_hits")
                if vector is None:
                    try:
                        vector = self.encode_record(record)
                    except SnapshotEmbeddingUnavailable as error:
                        self._report(error)
                        continue
                    self._persist(identity, fingerprint, vector)
                if spaces and vector.space not in spaces:
                    continue
                query_vector = self._query_vector(query, vector.space, query_vectors)
                if query_vector is None:
                    continue
                if (vector.model, vector.provider, len(vector.values), vector.identity_key) != (
                        query_vector.model, query_vector.provider, len(query_vector.values), query_vector.identity_key):
                    raise ValueError("Snapshot record and query embedding identities differ")
                values = np.asarray(vector.values, dtype=np.float32)
                query_values = np.asarray(query_vector.values, dtype=np.float32)
                norm = float(np.linalg.norm(values) * np.linalg.norm(query_values))
                if not norm or not np.isfinite(norm):
                    raise ValueError("Snapshot embedding has an invalid norm")
                score = float(np.dot(values, query_values) / norm)
                best = groups.setdefault((vector.identity_key, tuple(query_vector.values)), [])
                item = (score, -identity)
                if len(best) < limit:
                    heapq.heappush(best, item)
                elif item > best[0]:
                    heapq.heapreplace(best, item)
                counters.bump("retrieval_snapshot_vectors_scored")
        rankings = [[(-identity, score) for score, identity in sorted(best, reverse=True)]
                    for best in groups.values()]
        if len(rankings) == 1:
            return rankings[0]
        fused: dict[int, float] = {}
        scores: dict[int, float] = {}
        for ranking in rankings:
            for rank, (identity, score) in enumerate(ranking, 1):
                fused[identity] = fused.get(identity, 0.0) + 1.0 / (SNAPSHOT_RRF_K + rank)
                scores[identity] = max(scores.get(identity, score), score)
        return [(identity, scores[identity]) for identity in sorted(fused, key=lambda identity: (-fused[identity], identity))[:limit]]

    @staticmethod
    def _fingerprint(record: MemoryHit) -> str:
        return hashlib.sha256(json.dumps(
            [record.get("content"), record.get("context"), record.get("source_format")],
            ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()

    def _persist(self, identity: int, fingerprint: str, vector: SnapshotVector) -> None:
        values = np.asarray(vector.values, dtype=np.float32)
        if values.ndim != 1 or not values.size or not np.isfinite(values).all() or not vector.identity_key:
            raise ValueError(f"Invalid snapshot embedding for record {identity}")
        self.db.execute(
            "INSERT INTO snapshot_embedding_cache(knowledge_id,content_hash,embed_model,embedding_provider,"
            "embedding_space,float32_vector,embed_identity_key) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(knowledge_id) DO UPDATE SET content_hash=excluded.content_hash,embed_model=excluded.embed_model,"
            "embedding_provider=excluded.embedding_provider,embedding_space=excluded.embedding_space,"
            "float32_vector=excluded.float32_vector,embed_identity_key=excluded.embed_identity_key",
            (identity, fingerprint, vector.model, vector.provider, vector.space, values.tobytes(), vector.identity_key))
        counters.bump("retrieval_snapshot_vectors_rebuilt")
