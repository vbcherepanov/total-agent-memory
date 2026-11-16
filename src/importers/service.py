"""Store imported units through the normal save path, idempotently.

Every unit goes through ``Store.save_knowledge`` — private-tag stripping,
secret redaction, duplicate detection, embedding, graph linking — with
``repeat="confirm"`` so an older imported statement never supersedes a newer
stored one. The original time is then written to ``created_at`` /
``last_confirmed``, the fields temporal recall, timelines and reports read.
The import ledger (migration 042) records each unit's content hash, so a
re-run skips what an earlier run stored.

The LLM quality gate is skipped: an export holds thousands of turns and an
LLM verdict per turn would make a bulk import slow and, with a paid provider,
expensive. Trivial turns are filtered by ``MIN_TEXT_CHARS`` instead.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from importers.records import (
    MAX_RECORD_CHARS,
    MIN_TEXT_CHARS,
    ImportRecord,
    ImportStats,
    content_hash,
    split_text,
)
from memory_core.timestamps import format_utc, utc_now

LOGGER = logging.getLogger(__name__)
IMPORTED_TAG = "imported"
# A progress line is emitted after this many processed units.
PROGRESS_EVERY = 100
SESSION_SUFFIX_BYTES = 4

ProgressCallback = Callable[[ImportStats], None]


class SaveStore(Protocol):
    """The part of ``server.Store`` the importer uses."""

    db: Any
    cache: Any

    def session_start(self, sid: str, project: str = ..., branch: str = ...) -> None: ...

    def save_knowledge(self, sid: str, content: str, ktype: str, project: str = ..., tags: list[str] | None = ...,
                       context: str = ..., **kwargs: Any) -> tuple[Any, ...]: ...


class ImportLedger:
    """Read/write access to ``import_ledger``; a database without the table has no entries."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._available = self._has_table()

    def _has_table(self) -> bool:
        if self._db is None:
            return False
        try:
            self._db.execute("SELECT 1 FROM import_ledger LIMIT 1").fetchall()
        except sqlite3.OperationalError:
            return False
        return True

    def contains(self, kind: str, project: str, digest: str) -> bool:
        if not self._available:
            return False
        row = self._db.execute(
            "SELECT 1 FROM import_ledger WHERE kind=? AND project=? AND content_hash=?",
            (kind, project, digest),
        ).fetchone()
        return row is not None

    def record(self, kind: str, project: str, digest: str, knowledge_id: int | None, source_ref: str) -> None:
        if not self._available:
            raise RuntimeError("import_ledger table is missing; the store has not applied migration 042")
        self._db.execute(
            "INSERT OR IGNORE INTO import_ledger (kind, project, content_hash, knowledge_id, source_ref, imported_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (kind, project, digest, knowledge_id, source_ref, utc_now()),
        )

    def knowledge_id(self, kind: str, project: str, digest: str) -> int | None:
        row = self._db.execute(
            "SELECT knowledge_id FROM import_ledger WHERE kind=? AND project=? AND content_hash=?",
            (kind, project, digest)).fetchone()
        return int(row[0]) if row and row[0] is not None else None


class ImportService:
    """Feed ``ImportRecord`` units into a store, or count them in a dry run."""

    def __init__(self, *, kind: str, project: str, store: SaveStore | None, ledger_db: Any,
                 dry_run: bool, progress: ProgressCallback | None = None,
                 progress_every: int = PROGRESS_EVERY, min_chars: int = MIN_TEXT_CHARS,
                 max_chars: int | None = MAX_RECORD_CHARS) -> None:
        if not dry_run and store is None:
            raise ValueError("a store is required unless dry_run is set")
        if progress_every <= 0:
            raise ValueError("progress_every must be positive")
        self.kind = kind
        self.project = project
        self.store = store
        self.dry_run = dry_run
        self.ledger = ImportLedger(ledger_db)
        self.stats = ImportStats(kind=kind, project=project, dry_run=dry_run)
        self._progress = progress
        self._progress_every = progress_every
        self._min_chars = min_chars
        self._max_chars = max_chars
        self._processed = 0
        self._session_id = f"import-{kind}-{utc_now()[:19].replace(':', '')}-{secrets.token_hex(SESSION_SUFFIX_BYTES)}"
        self._session_started = False
        self.last_record_id: int | None = None

    def run(self, records: Iterable[ImportRecord]) -> ImportStats:
        """Import every record, then report and invalidate caches (also when a record fails)."""
        try:
            for record in records:
                self.add(record)
        finally:
            self.finish()
        return self.stats

    def add(self, record: ImportRecord) -> None:
        """Import one source record (split into parts when it is longer than ``max_chars``)."""
        self.last_record_id = None
        self._import(record)

    def finish(self) -> ImportStats:
        """Invalidate the query caches touched by this import and emit the final progress line."""
        if not self.dry_run and self._session_started:
            self._invalidate_caches()
        if self._progress is not None:
            self._progress(self.stats)
        return self.stats

    def _import(self, record: ImportRecord) -> None:
        text = record.text.strip()
        if not text or len(text) < self._min_chars:
            self.stats.skipped_short += 1
            self._tick()
            return
        parts = split_text(text, self._max_chars) if self._max_chars is not None else [text]
        if len(parts) > 1:
            self.stats.split_parts += len(parts)
        for number, part in enumerate(parts, start=1):
            context = record.context if len(parts) == 1 else f"{record.context} (part {number}/{len(parts)})"
            source_ref = record.source_ref if len(parts) == 1 else f"{record.source_ref}#part{number}"
            self._import_unit(record, part, context, source_ref)
        self._tick()

    def _import_unit(self, record: ImportRecord, text: str, context: str, source_ref: str) -> None:
        digest = content_hash(self.kind, self.project, text, record.occurred_at)
        if record.preserved_identity is not None:
            import hashlib

            digest = hashlib.sha256((digest + ":" + record.preserved_identity).encode()).hexdigest()
        if self.ledger.contains(self.kind, self.project, digest):
            self.last_record_id = self.ledger.knowledge_id(self.kind, self.project, digest)
            self.stats.already_imported += 1
            return
        self.stats.observe_time(record.occurred_at)
        if self.dry_run:
            self.stats.would_import += 1
            return
        store = self._require_store()
        if not self._session_started:
            store.session_start(self._session_id, project=self.project)
            self._session_started = True
        tags = list(dict.fromkeys((IMPORTED_TAG, f"import-{self.kind}", *record.tags)))
        record_id, deduplicated, redacted, _private, _quality = store.save_knowledge(
            self._session_id, text, record.knowledge_type, project=self.project, tags=tags, context=context,
            skip_quality=True, source_format=record.source_format, repeat="confirm",
            **({"skip_dedup": True} if record.preserved_identity is not None else {}),
        )
        if redacted:
            self.stats.redacted += 1
        if record_id is None:
            self.stats.rejected += 1
            LOGGER.warning(json.dumps({"event": "import_unit_rejected", "kind": self.kind, "source_ref": source_ref}))
            return
        if deduplicated:
            self.stats.deduplicated += 1
        else:
            self.stats.imported += 1
            if record.occurred_at is not None:
                stamp = format_utc(record.occurred_at)
                store.db.execute("UPDATE knowledge SET created_at=?, last_confirmed=? WHERE id=?",
                                 (stamp, stamp, record_id))
        self.last_record_id = int(record_id)
        self.ledger.record(self.kind, self.project, digest, int(record_id), source_ref)
        store.db.commit()

    def _tick(self) -> None:
        self._processed += 1
        if self._progress is not None and self._processed % self._progress_every == 0:
            self._progress(self.stats)

    def _require_store(self) -> SaveStore:
        if self.store is None:
            raise RuntimeError("this import runs without a store (dry run)")
        return self.store

    def _invalidate_caches(self) -> None:
        store = self._require_store()
        if getattr(store, "cache", None) is not None:
            store.cache.invalidate(project=self.project)
        v9_cache = getattr(store, "v9_cache", None)
        if v9_cache is not None:
            v9_cache.invalidate_all()
