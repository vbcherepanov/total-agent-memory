"""
Unified Ingestion Gateway — single entry point for all memory ingestion.

Orchestrates the full pipeline: queue -> chunk -> extract concepts -> enrich.
Uses raw SQLite for the ingest_queue table. No external HTTP dependencies
(uses urllib for URL fetching).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from ingestion.chunker import SemanticChunker
from ingestion.enricher import MetadataEnricher
from ingestion.extractor import ConceptExtractor
from queue_retry import ensure_column, next_attempt_iso, now_iso

LOG = lambda msg: sys.stderr.write(f"[memory-ingest] {msg}\n")

# An item whose processing raised is retried with backoff this many times
# before it is parked as 'error'.
MAX_INGEST_ATTEMPTS = 3
MAX_URL_BYTES = int(os.environ.get("MEMORY_INGEST_URL_MAX_BYTES", str(20 * 1024 * 1024)))
URL_TIMEOUT_SECONDS = float(os.environ.get("MEMORY_INGEST_URL_TIMEOUT_SECONDS", "10"))
URL_CONTENT_TYPES = frozenset({"text/html", "text/plain", "application/json", "application/xhtml+xml"})


def _now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    """Generate a new UUID hex string."""
    return uuid.uuid4().hex


class _SimpleHTMLStripper(HTMLParser):
    """Minimal HTML tag stripper — extracts visible text from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip: bool = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = False
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts).strip()


def _strip_html(html: str) -> str:
    """Strip HTML tags and return plain text."""
    parser = _SimpleHTMLStripper()
    parser.feed(html)
    return parser.get_text()


class IngestGateway:
    """Unified entry point for all memory ingestion.

    Orchestrates: ingest_queue -> chunking -> concept extraction -> metadata enrichment.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        ensure_column(db, "ingest_queue", "attempts", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "ingest_queue", "next_attempt_at", "TEXT")
        self.chunker = SemanticChunker()
        self.extractor = ConceptExtractor(db)
        self.enricher = MetadataEnricher(db)

    def ingest(
        self,
        content: str,
        source: str = "claude_mcp",
        content_type: str = "text",
        metadata: dict | None = None,
        raw_content: bytes | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        """Ingest new content into the system.

        1. Save to ingest_queue with status 'pending'
        2. Process immediately: chunk -> extract concepts -> enrich metadata
        3. Link to graph nodes

        Returns ingest_id.
        """
        ingest_id = self._receipt_id(idempotency_key) if idempotency_key is not None else _new_id()
        now = _now()
        meta_json = json.dumps(metadata) if metadata else None

        try:
            inserted = self.db.execute(
                """INSERT INTO ingest_queue
                   (id, source, content_type, raw_content, text_content, metadata,
                    status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                   ON CONFLICT(id) DO NOTHING""",
                (ingest_id, source, content_type, raw_content, content, meta_json, now),
            )
            self.db.commit()
            if inserted.rowcount == 0:
                self._require_receipt(ingest_id)
                return ingest_id
            LOG(f"Queued ingest item: {ingest_id} ({content_type}, {len(content)} chars)")
        except sqlite3.Error as exc:
            LOG(f"Failed to queue ingest item: {exc}")
            raise

        # Process immediately
        item = {
            "id": ingest_id,
            "source": source,
            "content_type": content_type,
            "raw_content": raw_content,
            "text_content": content,
            "metadata": metadata or {},
        }
        success = self._process_item(item)
        if not success:
            LOG(f"Immediate processing failed for {ingest_id}, left as pending")

        return ingest_id

    @staticmethod
    def _receipt_id(key: str) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, "tam-ingestion:" + key).hex

    def _require_receipt(self, ingest_id: str) -> None:
        row = self.db.execute("SELECT status FROM ingest_queue WHERE id = ?", (ingest_id,)).fetchone()
        if row is None or row[0] not in {"pending", "processing", "stored"}:
            raise ValueError("Ingestion receipt is missing or has failed")

    def ingest_url(self, url: str, metadata: dict | None = None, *, idempotency_key: str | None = None) -> str:
        """Fetch URL content and ingest.

        Uses urllib (no external deps) for basic fetching.
        Strips HTML tags to extract text content.
        """
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("URL must have an HTTP(S) scheme and hostname")
        if MAX_URL_BYTES < 1 or URL_TIMEOUT_SECONDS <= 0:
            raise ValueError("URL ingestion limits must be positive")
        if idempotency_key is not None:
            receipt_id = self._receipt_id(idempotency_key)
            if self.db.execute("SELECT 1 FROM ingest_queue WHERE id = ?", (receipt_id,)).fetchone():
                self._require_receipt(receipt_id)
                return receipt_id
        LOG(f"Fetching URL: {url}")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ClaudeMemory/5.0 (ingestion-gateway)",
                "Accept": "text/html,text/plain,application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=URL_TIMEOUT_SECONDS) as resp:
                content_type_header = resp.headers.get("Content-Type", "")
                media_type = content_type_header.partition(";")[0].strip().lower()
                if media_type and media_type not in URL_CONTENT_TYPES:
                    raise ValueError(f"Unsupported URL content type: {media_type}")
                raw_bytes = resp.read(MAX_URL_BYTES + 1)
                if len(raw_bytes) > MAX_URL_BYTES:
                    raise ValueError(f"URL content exceeds {MAX_URL_BYTES} bytes")
                charset = "utf-8"
                if "charset=" in content_type_header:
                    charset = content_type_header.split("charset=")[-1].strip().split(";")[0]

                raw_text = raw_bytes.decode(charset, errors="replace")
        except urllib.error.URLError as exc:
            LOG(f"URL fetch failed for {url}: {exc}")
            raise ValueError(f"Failed to fetch URL: {exc}") from exc
        except (OSError, UnicodeDecodeError) as exc:
            LOG(f"URL fetch error for {url}: {exc}")
            raise ValueError(f"Failed to process URL content: {exc}") from exc

        # Determine content type and extract text
        is_html = "text/html" in content_type_header or raw_text.strip().startswith("<")
        if is_html:
            text_content = _strip_html(raw_text)
            ct = "html"
        elif "application/json" in content_type_header:
            text_content = raw_text
            ct = "json"
        else:
            text_content = raw_text
            ct = "text"

        if not text_content.strip():
            raise ValueError(f"No text content extracted from {url}")

        # Merge URL into metadata
        meta = dict(metadata) if metadata else {}
        meta["source_url"] = url
        meta["original_content_type"] = content_type_header

        ingest_id = self.ingest(
            content=text_content,
            source="url",
            content_type=ct,
            metadata=meta,
            raw_content=raw_bytes,
            idempotency_key=idempotency_key,
        )
        self._require_receipt(ingest_id)
        return ingest_id

    def process_pending(self, limit: int = 10) -> int:
        """Process pending items in ingest_queue. Returns count processed."""
        try:
            rows = self.db.execute(
                """SELECT id, source, content_type, raw_content, text_content, metadata
                   FROM ingest_queue
                   WHERE status = 'pending'
                     AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (now_iso(), limit),
            ).fetchall()
        except sqlite3.Error as exc:
            LOG(f"Failed to fetch pending items: {exc}")
            return 0

        processed = 0
        for row in rows:
            item = {
                "id": row[0],
                "source": row[1],
                "content_type": row[2],
                "raw_content": row[3],
                "text_content": row[4],
                "metadata": json.loads(row[5]) if row[5] else {},
            }
            if self._process_item(item):
                processed += 1

        LOG(f"Processed {processed}/{len(rows)} pending items")
        return processed

    def _process_item(self, item: dict) -> bool:
        """Process a single ingest item. Returns True on success.

        Pipeline:
        1. Mark as 'processing'
        2. Chunk the content
        3. Extract concepts (fast mode for speed; deep if content is substantial)
        4. Enrich metadata
        5. Store results and mark as 'stored'
        """
        ingest_id = item["id"]
        text = item.get("text_content") or ""
        content_type = item.get("content_type", "text")
        metadata = item.get("metadata") or {}

        if not text.strip():
            self._update_status(ingest_id, "error", error_message="Empty content")
            return False

        self._update_status(ingest_id, "processing")

        try:
            # Step 1: Chunk
            if content_type == "code":
                chunks = self.chunker.chunk_code(text)
            else:
                chunks = self.chunker.chunk(text, content_type=content_type)

            if not chunks:
                chunks = [{"content": text, "index": 0, "token_estimate": len(text) // 4}]

            # Step 2: Enrich metadata
            enriched_meta = self.enricher.enrich(text, metadata)
            enriched_meta["chunk_count"] = len(chunks)
            enriched_meta["total_tokens"] = sum(c.get("token_estimate", 0) for c in chunks)

            # Step 3: Extract concepts
            # Use deep extraction for substantial content (>200 tokens), fast otherwise
            use_deep = enriched_meta.get("total_tokens", 0) > 200
            extraction = self.extractor.extract_and_link(text, deep=use_deep)

            # Step 4: Store processing results
            result_meta = {
                **enriched_meta,
                "chunks": chunks,
                "extraction": {
                    "concepts": extraction.get("concepts", []),
                    "entities": extraction.get("entities", []),
                    "relations": extraction.get("relations", []),
                    "capabilities": extraction.get("capabilities", []),
                    "key_patterns": extraction.get("key_patterns", []),
                },
            }

            self.db.execute(
                """UPDATE ingest_queue
                   SET status = 'stored', metadata = ?, processed_at = ?
                   WHERE id = ?""",
                (json.dumps(result_meta), _now(), ingest_id),
            )
            self.db.commit()

            concept_count = len(extraction.get("concepts", []))
            entity_count = len(extraction.get("entities", []))
            LOG(
                f"Processed {ingest_id}: {len(chunks)} chunks, "
                f"{concept_count} concepts, {entity_count} entities"
            )
            return True

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            LOG(f"Processing failed for {ingest_id}: {error_msg}")
            self._schedule_retry_or_fail(ingest_id, error_msg)
            return False

    def _schedule_retry_or_fail(self, ingest_id: str, error_msg: str) -> None:
        """Re-queue a failed item behind an exponential backoff; park it as 'error' once exhausted."""
        try:
            row = self.db.execute(
                "SELECT attempts FROM ingest_queue WHERE id = ?", (ingest_id,)
            ).fetchone()
            attempts = ((row[0] if row else 0) or 0) + 1
            if attempts >= MAX_INGEST_ATTEMPTS:
                self.db.execute(
                    "UPDATE ingest_queue SET status = 'error', attempts = ?, error_message = ?, "
                    "processed_at = ?, next_attempt_at = NULL WHERE id = ?",
                    (attempts, error_msg, _now(), ingest_id),
                )
            else:
                self.db.execute(
                    "UPDATE ingest_queue SET status = 'pending', attempts = ?, error_message = ?, "
                    "next_attempt_at = ? WHERE id = ?",
                    (attempts, error_msg, next_attempt_iso(attempts), ingest_id),
                )
            self.db.commit()
        except sqlite3.Error as exc:
            LOG(f"Failed to schedule retry for {ingest_id}: {exc}")

    def _update_status(
        self,
        ingest_id: str,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Update ingest_queue item status."""
        try:
            if error_message:
                self.db.execute(
                    """UPDATE ingest_queue
                       SET status = ?, error_message = ?, processed_at = ?
                       WHERE id = ?""",
                    (status, error_message, _now(), ingest_id),
                )
            else:
                self.db.execute(
                    "UPDATE ingest_queue SET status = ? WHERE id = ?",
                    (status, ingest_id),
                )
            self.db.commit()
        except sqlite3.Error as exc:
            LOG(f"Failed to update status for {ingest_id}: {exc}")

    def get_queue_status(self) -> dict:
        """Return queue stats: pending, processing, stored, error counts."""
        stats: dict[str, Any] = {
            "pending": 0,
            "processing": 0,
            "stored": 0,
            "error": 0,
            "total": 0,
        }

        try:
            rows = self.db.execute(
                "SELECT status, COUNT(*) as cnt FROM ingest_queue GROUP BY status"
            ).fetchall()
            for row in rows:
                status = row[0]
                count = row[1]
                if status in stats:
                    stats[status] = count
                stats["total"] += count
        except sqlite3.Error as exc:
            LOG(f"Failed to get queue status: {exc}")

        return stats
