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
from datetime import datetime, timezone
from typing import Callable

try:
    from multi_repr_store import MultiReprStore, VALID_REPRESENTATIONS, content_hash
    from validator import ContentValidator
except ImportError:  # package import fallback
    from .multi_repr_store import MultiReprStore, VALID_REPRESENTATIONS, content_hash  # type: ignore[no-redef]
    from .validator import ContentValidator  # type: ignore[no-redef]

LOG = lambda msg: sys.stderr.write(f"[repr-queue] {msg}\n")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Generator: (content, project=None) -> {"summary","keywords","questions"}
GeneratorFn = Callable[..., dict[str, str]]
# Embedder: (text) -> list[float]
EmbedderFn = Callable[[str], list[float]]


class RepresentationsQueue:
    """Queue for generating + embedding multi-view representations of knowledge."""

    def __init__(self, db: sqlite3.Connection, max_attempts: int = 3) -> None:
        self.db = db
        self.max_attempts = max(1, max_attempts)
        self.store = MultiReprStore(db)
        self.validator = ContentValidator()

    def enqueue(self, knowledge_id: int) -> bool:
        existing = self.db.execute(
            "SELECT id FROM representations_queue "
            "WHERE knowledge_id=? AND status='pending' LIMIT 1",
            (knowledge_id,),
        ).fetchone()
        if existing:
            return False
        self.db.execute(
            "INSERT INTO representations_queue (knowledge_id, status, created_at) "
            "VALUES (?, 'pending', ?)",
            (knowledge_id, _now()),
        )
        self.db.commit()
        return True

    def claim_next(self) -> dict | None:
        row = self.db.execute(
            "SELECT id, knowledge_id, attempts FROM representations_queue "
            "WHERE status='pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        self.db.execute(
            "UPDATE representations_queue SET status='processing', claimed_at=? WHERE id=?",
            (_now(), row["id"]),
        )
        self.db.commit()
        return {
            "id": row["id"],
            "knowledge_id": row["knowledge_id"],
            "attempts": row["attempts"],
        }

    def mark_done(self, item_id: int) -> None:
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
            "UPDATE representations_queue SET status='done', processed_at=? WHERE id=?",
            (_now(), item_id),
        )
        self.db.commit()

    def mark_failed(self, item_id: int, error: str) -> None:
        row = self.db.execute(
            "SELECT attempts, knowledge_id FROM representations_queue WHERE id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            return
        next_attempts = (row["attempts"] or 0) + 1
        new_status = "failed" if next_attempts >= self.max_attempts else "pending"
        self.db.execute(
            "DELETE FROM representations_queue "
            "WHERE knowledge_id = ? AND status = ? AND id != ?",
            (row["knowledge_id"], new_status, item_id),
        )
        self.db.execute(
            "UPDATE representations_queue "
            "SET status=?, attempts=?, last_error=?, processed_at=? WHERE id=?",
            (new_status, next_attempts, (error or "")[:500], _now(), item_id),
        )
        self.db.commit()

    # ──────────────────────────────────────────────
    # Worker
    # ──────────────────────────────────────────────

    def process_pending(
        self,
        generator: GeneratorFn,
        embedder: EmbedderFn,
        model_name: str,
        limit: int = 10,
    ) -> dict[str, int]:
        stats = {"processed": 0, "failed": 0, "skipped": 0}

        for _ in range(limit):
            item = self.claim_next()
            if item is None:
                break

            kid = item["knowledge_id"]
            content_row = self.db.execute(
                "SELECT content FROM knowledge WHERE id=?", (kid,)
            ).fetchone()
            if content_row is None:
                self.mark_done(item["id"])
                stats["skipped"] += 1
                continue

            raw_content = content_row["content"] or ""
            # Hash the parent content once per drain — recall uses it to
            # detect drift between the saved view and the current parent.
            parent_hash = content_hash(raw_content)
            try:
                # Raw embedding — always generated; serves as fallback
                raw_emb = embedder(raw_content)
                if raw_emb:
                    self.store.upsert(
                        kid, "raw", raw_content, raw_emb, model_name,
                        parent_content_hash=parent_hash,
                    )

                # LLM views (summary/keywords/questions/compressed)
                views = generator(raw_content) or {}
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
                    if emb:
                        self.store.upsert(
                            kid, name, text, emb, model_name,
                            parent_content_hash=parent_hash,
                        )

                self.mark_done(item["id"])
                stats["processed"] += 1
            except Exception as e:  # noqa: BLE001
                LOG(f"repr generation failed for knowledge_id={kid}: {e}")
                self.mark_failed(item["id"], str(e))
                stats["failed"] += 1

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
        return out
