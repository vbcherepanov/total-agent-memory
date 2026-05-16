"""Multi-representation embedding store.

Stores multiple embedding "views" of the same knowledge record — raw content,
summary, keywords, utility questions — so retrieval can match on any of them
and fuse scores via Reciprocal Rank Fusion (RRF).

Schema lives in migrations/002_multi_representation.sql. Backward-compatible:
existing `embeddings` table is left untouched.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from datetime import datetime, timezone
from typing import Iterable


VALID_REPRESENTATIONS: frozenset[str] = frozenset(
    {"raw", "summary", "keywords", "questions", "compressed", "criterion"}
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def content_hash(content: str | None) -> str:
    """Stable sha256 hex digest of parent content. Used for drift detection
    so recall can dampen hits via representations whose parent record has
    since been edited."""
    if content is None:
        content = ""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _float32_blob(vec: Iterable[float]) -> bytes:
    vec = list(vec)
    return struct.pack(f"{len(vec)}f", *vec)


def _binary_blob(vec: Iterable[float]) -> bytes:
    """Quantize to packed uint8 (sign-bits → bytes). Matches server._quantize_binary."""
    import numpy as np

    arr = np.array(list(vec), dtype=np.float32)
    bits = (arr > 0).astype(np.uint8)
    return np.packbits(bits).tobytes()


class MultiReprStore:
    """CRUD for knowledge_representations table."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def upsert(
        self,
        knowledge_id: int,
        representation: str,
        content: str,
        embedding: list[float],
        model: str,
        parent_content_hash: str | None = None,
    ) -> None:
        """Insert or replace a single (knowledge_id, representation) row.

        ``parent_content_hash`` is the sha256 of the *parent* knowledge.content
        at the moment this view was generated. Recall compares it against the
        current parent hash to detect drift (parent edited but view stale).
        Backward compatible: callers that don't supply it leave the column
        NULL and drift checks become a no-op for that row.
        """
        if representation not in VALID_REPRESENTATIONS:
            raise ValueError(
                f"representation must be one of {sorted(VALID_REPRESENTATIONS)}, "
                f"got {representation!r}"
            )
        if not embedding:
            raise ValueError("embedding cannot be empty")

        # Schema-detect: column may be missing on legacy DBs that haven't
        # applied migration 027 yet. Fall back to the legacy INSERT shape.
        has_hash_col = self._has_hash_column()
        now = _now()

        if has_hash_col:
            self.db.execute(
                """INSERT INTO knowledge_representations
                     (knowledge_id, representation, content, binary_vector,
                      float32_vector, embed_model, embed_dim, created_at,
                      parent_content_hash, last_confirmed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(knowledge_id, representation) DO UPDATE SET
                     content             = excluded.content,
                     binary_vector       = excluded.binary_vector,
                     float32_vector      = excluded.float32_vector,
                     embed_model         = excluded.embed_model,
                     embed_dim           = excluded.embed_dim,
                     created_at          = excluded.created_at,
                     parent_content_hash = excluded.parent_content_hash,
                     last_confirmed      = excluded.last_confirmed""",
                (
                    knowledge_id,
                    representation,
                    content,
                    _binary_blob(embedding),
                    _float32_blob(embedding),
                    model,
                    len(embedding),
                    now,
                    parent_content_hash,
                    now,
                ),
            )
        else:
            self.db.execute(
                """INSERT INTO knowledge_representations
                     (knowledge_id, representation, content, binary_vector,
                      float32_vector, embed_model, embed_dim, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(knowledge_id, representation) DO UPDATE SET
                     content        = excluded.content,
                     binary_vector  = excluded.binary_vector,
                     float32_vector = excluded.float32_vector,
                     embed_model    = excluded.embed_model,
                     embed_dim      = excluded.embed_dim,
                     created_at     = excluded.created_at""",
                (
                    knowledge_id,
                    representation,
                    content,
                    _binary_blob(embedding),
                    _float32_blob(embedding),
                    model,
                    len(embedding),
                    now,
                ),
            )
        self.db.commit()

    def _has_hash_column(self) -> bool:
        try:
            rows = self.db.execute(
                "PRAGMA table_info(knowledge_representations)"
            ).fetchall()
            return any(r[1] == "parent_content_hash" for r in rows)
        except sqlite3.Error:
            return False

    def get_all_for(self, knowledge_id: int) -> list[dict]:
        rows = self.db.execute(
            """SELECT representation, content, embed_model, embed_dim, created_at
                 FROM knowledge_representations
                WHERE knowledge_id = ?
                ORDER BY representation""",
            (knowledge_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_all_for(self, knowledge_id: int) -> int:
        cur = self.db.execute(
            "DELETE FROM knowledge_representations WHERE knowledge_id = ?",
            (knowledge_id,),
        )
        self.db.commit()
        return cur.rowcount

    def count_by_type(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT representation, COUNT(*) AS c FROM knowledge_representations "
            "GROUP BY representation"
        ).fetchall()
        return {r["representation"]: r["c"] for r in rows}


# ──────────────────────────────────────────────
# RRF fusion
# ──────────────────────────────────────────────


def rrf_fuse(
    ranked: dict[str, list[tuple[int, float]]],
    k: int = 60,
    top_n: int = 10,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion across multiple ranked result lists.

    `ranked` maps representation name → list of (knowledge_id, raw_score) ordered
    by raw_score descending. Raw scores are only used to order within each list;
    RRF ignores magnitudes and uses rank only. Returns up to top_n
    (knowledge_id, fused_score) pairs, highest first.
    """
    scores: dict[int, float] = {}
    for _repr, items in ranked.items():
        # Ensure items are sorted by raw score desc — caller may already have done so
        ordered = sorted(items, key=lambda kv: kv[1], reverse=True)
        for rank, (kid, _raw) in enumerate(ordered):
            scores[kid] = scores.get(kid, 0.0) + 1.0 / (k + rank + 1)
    fused = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return fused[:top_n]
