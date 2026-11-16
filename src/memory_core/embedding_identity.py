"""Identity of the vectors a store holds, and detection of stale ones.

A vector belongs to a group ``(space, model, dim)``. The model name alone does
not say how the vector was made: a fallback backend can answer under the
configured name, and a library upgrade can change pooling under an unchanged
name. This module keeps three things honest:

* :class:`EmbedIdentity`: the backend, model and recipe that really produced a
  vector, recorded next to it instead of the configured name;
* :func:`stale_ids` / :func:`count_stale`: active records whose stored model is
  not the model that serves their space today;
* the fingerprint table (migration 044): a probe vector per group; when
  today's output for the probe text no longer matches, the group is retired
  with :func:`retire_group` and the normal stale path re-embeds it.
"""

from __future__ import annotations

import math
import hashlib
import json
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from memory_core.timestamps import utc_now

DEFAULT_SPACE = "text"
STALE_MODEL_SUFFIX = "#stale"
PROBE_TEXT = "Памятка: перенос деплоя на четверг / deployment moved to Thursday."
# Same recipe gives the same unit vector up to float noise; a pooling or
# preprocessing change moves it far below this.
MIN_PROBE_COSINE = 0.999
MAX_STALE_ENQUEUE = 10_000
FASTEMBED_MEAN_POOLING_FROM = (0, 6)


class FingerprintState(StrEnum):
    NEW = "new"
    SAME = "same"
    CHANGED = "changed"


@dataclass(frozen=True)
class EmbedIdentity:
    """What actually produced a vector."""

    model: str
    provider: str
    pooling: str = ""
    library: str = ""

    def key(self, dimension: int) -> str:
        if not self.model or self.model == 'auto' or not self.provider or dimension <= 0:
            raise ValueError('Embedding identity requires a concrete model, provider and dimension')
        payload = [self.model, self.provider, self.pooling, self.library, dimension]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def fastembed_pooling(version: str) -> str:
    """Pooling fastembed applies by default for the sentence-transformers models it serves."""
    parts = tuple(int(piece) for piece in version.split(".")[:2] if piece.isdigit())
    return "mean" if parts >= FASTEMBED_MEAN_POOLING_FROM else "cls"


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _mismatch_clause(expected: Mapping[str, str]) -> tuple[str, list[Any]]:
    clauses, params = [], []
    for space, model in expected.items():
        clauses.append("(COALESCE(e.embedding_space,'text')=? AND e.embed_model<>?)")
        params.extend((space, model))
    return " OR ".join(clauses), params


def stale_ids(db: Any, expected: Mapping[str, str], limit: int = MAX_STALE_ENQUEUE) -> list[int]:
    """Active records whose vector was made by a model other than ``expected[space]``."""
    if not expected:
        return []
    clause, params = _mismatch_clause(expected)
    rows = db.execute(
        "SELECT DISTINCT k.id FROM embeddings e JOIN knowledge k ON k.id=e.knowledge_id "
        f"WHERE k.status='active' AND ({clause}) ORDER BY k.id LIMIT ?",
        (*params, int(limit)),
    ).fetchall()
    return [int(row[0]) for row in rows]


def reconciliation_ids(db: Any, expected: Mapping[str, str], limit: int = MAX_STALE_ENQUEUE) -> list[int]:
    if not expected or limit <= 0:
        return []
    clause, params = _mismatch_clause(expected)
    rows = db.execute(
        "SELECT k.id FROM knowledge k LEFT JOIN embeddings e ON e.knowledge_id=k.id "
        "LEFT JOIN embedding_pending p ON p.knowledge_id=k.id "
        "WHERE k.status='active' AND p.knowledge_id IS NULL AND (e.knowledge_id IS NULL "
        f"OR e.embed_model IS NULL OR e.embed_model IN ('','auto') OR ({clause})) "
        "ORDER BY k.id LIMIT ?", (*params, int(limit)),
    ).fetchall()
    return [int(row[0]) for row in rows]


@dataclass(frozen=True)
class EmbeddingCoverage:
    active: int
    missing: int
    unknown: int
    stale: int
    pending: int
    failed: int
    unqueued: int


def embedding_coverage(db: Any, expected: Mapping[str, str]) -> EmbeddingCoverage:
    clause, params = _mismatch_clause(expected)
    stale = f"e.embed_model NOT IN ('','auto') AND ({clause})" if clause else "1=0"
    rows = db.execute(
        "SELECT gap, COALESCE(queue_status,''), COUNT(*) FROM (SELECT "
        "CASE WHEN e.knowledge_id IS NULL THEN 'missing' "
        "WHEN e.embed_model IS NULL OR e.embed_model IN ('','auto') THEN 'unknown' "
        f"WHEN {stale} THEN 'stale' ELSE 'compatible' END AS gap, p.status AS queue_status "
        "FROM knowledge k LEFT JOIN embeddings e ON e.knowledge_id=k.id "
        "LEFT JOIN embedding_pending p ON p.knowledge_id=k.id WHERE k.status='active') coverage "
        "GROUP BY gap, queue_status", params,
    ).fetchall()
    counts = {name: 0 for name in EmbeddingCoverage.__dataclass_fields__}
    for gap, status, count in rows:
        counts['active'] += count
        if gap != 'compatible':
            counts[gap] += count
            if not status:
                counts['unqueued'] += count
        if status in {'pending', 'failed'}:
            counts[status] += count
    return EmbeddingCoverage(**counts)


def count_stale(db: Any, space: str, model: str, dimension: int, project: str | None = None) -> int:
    """Active records with a vector in the group ``(space, model, dimension)``."""
    sql = (
        "SELECT COUNT(*) FROM embeddings e JOIN knowledge k ON k.id=e.knowledge_id "
        "WHERE k.status='active' AND COALESCE(e.embedding_space,'text')=? "
        "AND e.embed_model=? AND e.embed_dim=?"
    )
    params: list[Any] = [space, model, int(dimension)]
    if project is not None:
        sql += " AND k.project=?"
        params.append(project)
    return int(db.execute(sql, params).fetchone()[0])


def check_fingerprint(db: Any, space: str, model: str, probe: Sequence[float]) -> FingerprintState:
    """Compare today's probe vector with the one recorded for ``(space, model)``."""
    row = db.execute(
        "SELECT probe_vector FROM embedding_fingerprints WHERE space=? AND model=?", (space, model)
    ).fetchone()
    if row is None:
        return FingerprintState.NEW
    return FingerprintState.SAME if cosine(_unpack(row[0]), probe) >= MIN_PROBE_COSINE else FingerprintState.CHANGED


def record_fingerprint(db: Any, space: str, identity: EmbedIdentity, probe: Sequence[float]) -> None:
    db.execute(
        "INSERT OR REPLACE INTO embedding_fingerprints "
        "(space, model, provider, dim, pooling, library, probe_vector, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
        (space, identity.model, identity.provider, len(probe), identity.pooling, identity.library,
         _pack(probe), utc_now()),
    )
    db.commit()


def retire_group(db: Any, space: str, model: str) -> int:
    """Relabel a group's vectors so no search treats them as ``model`` output; returns the row count."""
    cursor = db.execute(
        "UPDATE embeddings SET embed_model=? WHERE COALESCE(embedding_space,'text')=? AND embed_model=?",
        (model + STALE_MODEL_SUFFIX, space, model),
    )
    if space == DEFAULT_SPACE:
        db.execute("UPDATE knowledge_representations SET embed_model=? WHERE embed_model=?",
                   (model + STALE_MODEL_SUFFIX, model))
    db.commit()
    return int(cursor.rowcount or 0)
