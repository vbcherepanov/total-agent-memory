"""Async queue for generating & embedding multi-representation views.

memory_save enqueues a knowledge_id; reflection.agent (or a dedicated cron)
drains the queue, calls representations.generate_representations() for the
LLM-derived views (summary/keywords/questions), embeds each with the same
embedder as raw content, and writes them to knowledge_representations
(see migration 002).

This separates the slow LLM+embedding work from the fast save path.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, TypedDict

from queue_retry import ensure_column, next_attempt_iso, now_iso, STALE_PROCESSING_MINUTES
from memory_core.embedding_identity import EmbedIdentity
from memory_core.telemetry import counters, op_timer

try:
    from multi_repr_store import MultiReprStore, RepresentationSnapshot, VALID_REPRESENTATIONS, content_hash
    from validator import ContentValidator
except ImportError:  # package import fallback
    from .multi_repr_store import MultiReprStore, RepresentationSnapshot, VALID_REPRESENTATIONS, content_hash  # type: ignore[no-redef]
    from .validator import ContentValidator  # type: ignore[no-redef]

LOG = lambda msg: sys.stderr.write(f"[repr-queue] {msg}\n")
RECONCILE_BATCH_SIZE = 1000


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Generator: (content, project=None) -> {"summary","keywords","questions"}
GeneratorFn = Callable[..., dict[str, str]]
# Embedder: (text) -> list[float]
EmbedderFn = Callable[[str], list[float]]


class RepresentationClaim(TypedDict):
    id: int
    knowledge_id: int
    attempts: int
    claim_token: str


@dataclass(frozen=True)
class GeneratedView:
    name: str
    content: str
    embedding: list[float]
    identity: EmbedIdentity


@dataclass(frozen=True)
class ReembeddedView:
    snapshot: RepresentationSnapshot
    embedding: list[float]
    identity: EmbedIdentity


class RepresentationsQueue:
    """Queue for generating + embedding multi-view representations of knowledge."""

    def __init__(self, db: sqlite3.Connection, max_attempts: int = 3) -> None:
        self.db = db
        self.max_attempts = max(1, max_attempts)
        self.store = MultiReprStore(db)
        self.validator = ContentValidator()
        ensure_column(db, "representations_queue", "next_attempt_at", "TEXT")
        ensure_column(db, "representations_queue", "claim_token", "TEXT")
        self._claims: dict[int, str] = {}

    def enqueue(self, knowledge_id: int) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO representations_queue (knowledge_id, status, created_at) "
            "VALUES (?, 'pending', ?)",
            (knowledge_id, _now()),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def enqueue_incompatible(self, identity: EmbedIdentity, limit: int = RECONCILE_BATCH_SIZE) -> int:
        if limit <= 0:
            return 0
        dimensions = self.db.execute('SELECT DISTINCT embed_dim FROM knowledge_representations').fetchall()
        added = 0
        for dimension in dimensions:
            key = identity.key(int(dimension[0])) if dimension[0] and dimension[0] > 0 else ''
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO representations_queue (knowledge_id,status,created_at) "
                "SELECT DISTINCT kr.knowledge_id,'pending',? FROM knowledge_representations kr "
                "JOIN knowledge k ON k.id=kr.knowledge_id WHERE k.status='active' AND kr.embed_dim=? "
                "AND (kr.embed_model<>? OR kr.embed_identity_key IS NULL OR kr.embed_identity_key<>?) "
                "AND NOT EXISTS (SELECT 1 FROM representations_queue q WHERE q.knowledge_id=kr.knowledge_id "
                "AND q.status IN ('pending','processing','failed')) ORDER BY kr.knowledge_id LIMIT ?",
                (now_iso(), dimension[0], identity.model, key, limit - added),
            )
            added += cursor.rowcount
            if added >= limit:
                break
        self.db.commit()
        return added

    def reclaim_stale(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_PROCESSING_MINUTES)).isoformat()
        try:
            rows = self.db.execute(
                "UPDATE representations_queue SET claim_token=NULL "
                "WHERE status='processing' AND (claimed_at IS NULL OR claimed_at < ?) "
                "RETURNING id, knowledge_id, attempts", (cutoff,),
            ).fetchall()
            for row in rows:
                pending = self.db.execute(
                    "SELECT 1 FROM representations_queue WHERE knowledge_id=? AND status='pending'",
                    (row["knowledge_id"],),
                ).fetchone()
                if pending:
                    self.db.execute("DELETE FROM representations_queue WHERE id=?", (row["id"],))
                    continue
                attempts = row["attempts"] + 1
                status = "failed" if attempts >= self.max_attempts else "pending"
                self.db.execute(
                    "DELETE FROM representations_queue WHERE knowledge_id=? AND status=? AND id!=?",
                    (row["knowledge_id"], status, row["id"]),
                )
                self.db.execute(
                    "UPDATE representations_queue SET status=?, attempts=?, claimed_at=NULL, "
                    "next_attempt_at=NULL, last_error='reclaimed: worker did not finish' WHERE id=?",
                    (status, attempts, row["id"]),
                )
            self.db.commit()
            return len(rows)
        except sqlite3.Error:
            self.db.rollback()
            raise

    def claim_next(self) -> RepresentationClaim | None:
        self.reclaim_stale()
        try:
            row = self.db.execute(
                "UPDATE representations_queue SET status='processing', claimed_at=?, claim_token=? "
                "WHERE id=(SELECT pending.id FROM representations_queue pending "
                "WHERE pending.status='pending' AND (pending.next_attempt_at IS NULL OR pending.next_attempt_at <= ?) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM representations_queue running WHERE "
                "running.knowledge_id=pending.knowledge_id AND running.status='processing') "
                "ORDER BY pending.id LIMIT 1) AND status='pending' "
                "RETURNING id, knowledge_id, attempts, claim_token",
                (now_iso(), uuid.uuid4().hex, now_iso()),
            ).fetchone()
            self.db.commit()
        except sqlite3.Error:
            self.db.rollback()
            raise
        if row is None:
            return None
        item = RepresentationClaim(id=row["id"], knowledge_id=row["knowledge_id"],
                                   attempts=row["attempts"], claim_token=row["claim_token"])
        self._claims[item["id"]] = item["claim_token"]
        return item

    def _lock_claim(self, item_id: int, claim_token: str | None) -> bool:
        token = claim_token or self._claims.get(item_id)
        return self.db.execute(
            "UPDATE representations_queue SET claimed_at=? WHERE id=? AND status='processing' AND claim_token=?",
            (now_iso(), item_id, token),
        ).rowcount == 1

    def mark_done(self, item_id: int, *, claim_token: str | None = None) -> bool:
        if not self._lock_claim(item_id, claim_token):
            self.db.rollback()
            return False
        row = self.db.execute(
            "SELECT knowledge_id FROM representations_queue WHERE id=?", (item_id,)
        ).fetchone()
        if row is not None:
            self.db.execute(
                "DELETE FROM representations_queue "
                "WHERE knowledge_id = ? AND status = 'done' AND id != ?",
                (row["knowledge_id"], item_id),
            )
        self.db.execute(
            "UPDATE representations_queue SET status='done', processed_at=?, claim_token=NULL WHERE id=?",
            (_now(), item_id),
        )
        self.db.commit()
        self._claims.pop(item_id, None)
        return True

    def mark_failed(self, item_id: int, error: str, *, claim_token: str | None = None) -> bool:
        if not self._lock_claim(item_id, claim_token):
            self.db.rollback()
            return False
        row = self.db.execute(
            "SELECT attempts, knowledge_id FROM representations_queue WHERE id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            self.db.rollback()
            return False
        next_attempts = (row["attempts"] or 0) + 1
        new_status = "failed" if next_attempts >= self.max_attempts else "pending"
        self.db.execute(
            "DELETE FROM representations_queue "
            "WHERE knowledge_id = ? AND status = ? AND id != ?",
            (row["knowledge_id"], new_status, item_id),
        )
        self.db.execute(
            "UPDATE representations_queue "
            "SET status=?, attempts=?, last_error=?, processed_at=?, next_attempt_at=?, "
            "claimed_at=NULL, claim_token=NULL WHERE id=?",
            (new_status, next_attempts, (error or "")[:500], _now(),
             next_attempt_iso(next_attempts) if new_status == "pending" else None, item_id),
        )
        self.db.commit()
        self._claims.pop(item_id, None)
        return True

    # ──────────────────────────────────────────────
    # Worker
    # ──────────────────────────────────────────────

    @op_timer('representation_reembed_ms')
    def _reembed_views(self, views: list[RepresentationSnapshot], embedder: EmbedderFn,
                      identity: EmbedIdentity, dimension: int,
                      identity_of: Callable[[], EmbedIdentity | None] | None) -> list[ReembeddedView]:
        expected = identity.key(dimension)
        repaired = []
        for view in views:
            if (view.embed_model == identity.model and view.embed_dim == dimension
                    and view.embed_identity_key == expected):
                continue
            embedding = embedder(view.content)
            actual = identity_of() if identity_of else identity
            if not embedding or actual is None or actual.key(len(embedding)) != expected:
                raise ValueError('Embedding provider changed during representation repair')
            repaired.append(ReembeddedView(view, embedding, actual))
        return repaired

    def process_pending(
        self,
        generator: GeneratorFn,
        embedder: EmbedderFn,
        model_name: str,
        limit: int = 10,
        *,
        identity_of: Callable[[], EmbedIdentity | None] | None = None,
    ) -> dict[str, int]:
        stats = {"processed": 0, "failed": 0, "skipped": 0, "lost_claims": 0}

        for _ in range(limit):
            item = self.claim_next()
            if item is None:
                break

            kid = item["knowledge_id"]
            content_row = self.db.execute(
                "SELECT content FROM knowledge WHERE id=? AND status='active'", (kid,)
            ).fetchone()
            if content_row is None:
                self.mark_done(item["id"], claim_token=item["claim_token"])
                stats["skipped"] += 1
                continue

            raw_content = content_row["content"] or ""
            # Hash the parent content once per drain — recall uses it to
            # detect drift between the saved view and the current parent.
            parent_hash = content_hash(raw_content)
            try:
                # Raw embedding — always generated; serves as fallback
                raw_emb = embedder(raw_content)
                if not raw_emb:
                    raise ValueError("Raw representation embedding is unavailable")
                identity = identity_of() if identity_of else EmbedIdentity(model_name, 'declared')
                if identity is None:
                    raise ValueError('Embedding provider did not report its identity')
                identity.key(len(raw_emb))
                repaired = self._reembed_views(self.store.snapshots(kid), embedder, identity,
                                               len(raw_emb), identity_of)
                generated = [] if repaired else [GeneratedView("raw", raw_content, raw_emb, identity)]

                # LLM views (summary/keywords/questions/compressed)
                views = {} if repaired else generator(raw_content) or {}
                for name in ("summary", "keywords", "questions", "compressed"):
                    text = str(views.get(name, "") or "").strip()
                    if not text:
                        continue
                    if name not in VALID_REPRESENTATIONS:
                        continue
                    # Validator guard: compressed MUST preserve URLs/paths/code.
                    # Reject silently if LLM dropped anything critical — raw stays.
                    if name == "compressed":
                        v = self.validator.validate(raw_content, text)
                        if not v.ok:
                            LOG(
                                f"compressed rejected for kid={kid}: "
                                f"{'; '.join(v.errors[:2])}"
                            )
                            continue
                    emb = embedder(text)
                    if not emb:
                        raise ValueError(f"Embedding is unavailable for {name}")
                    view_identity = identity_of() if identity_of else identity
                    if view_identity is None or view_identity.key(len(emb)) != identity.key(len(raw_emb)):
                        raise ValueError('Embedding provider changed during representation generation')
                    generated.append(GeneratedView(name, text, emb, view_identity))

                if not self._lock_claim(item["id"], item["claim_token"]):
                    self.db.rollback()
                    stats["skipped"] += 1
                    continue
                current = self.db.execute(
                    "SELECT content FROM knowledge WHERE id=? AND status='active'", (kid,),
                ).fetchone()
                if current is None or content_hash(current["content"]) != parent_hash:
                    raise ValueError("Parent content changed during representation generation")
                for view in repaired:
                    if not self.store.replace_embedding(view.snapshot, view.embedding, view.identity):
                        counters.bump('representation_reembed_conflicts')
                        raise ValueError('Representation changed during embedding repair')
                for view in generated:
                    self.store.upsert(kid, view.name, view.content, view.embedding, view.identity.model,
                                      parent_content_hash=parent_hash, commit=False, identity=view.identity)
                self.mark_done(item["id"], claim_token=item["claim_token"])
                counters.bump('representation_reembed_updated', len(repaired))
                stats["processed"] += 1
            except Exception as e:  # noqa: BLE001
                self.db.rollback()
                LOG(f"repr generation failed for knowledge_id={kid}: {e}")
                if self.mark_failed(item["id"], str(e), claim_token=item["claim_token"]):
                    stats["failed"] += 1
                else:
                    stats["lost_claims"] += 1

        return stats

    # ──────────────────────────────────────────────
    # Observability
    # ──────────────────────────────────────────────

    def stats(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT status, COUNT(*) AS c FROM representations_queue GROUP BY status"
        ).fetchall()
        out = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
        for r in rows:
            out[r["status"]] = r["c"]
        oldest = self.db.execute(
            "SELECT MIN(claimed_at) FROM representations_queue WHERE status='processing'",
        ).fetchone()[0]
        out["oldest_processing_seconds"] = (
            max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds()))
            if oldest else 0
        )
        return out
