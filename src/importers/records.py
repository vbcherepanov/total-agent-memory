"""Shared types for `tam import`: the unit an importer yields, counters, text helpers."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from memory_core.timestamps import format_utc

# A single stored record never exceeds this many characters; longer texts are
# split on paragraph, then line, then hard boundaries into "part N/M" records.
MAX_RECORD_CHARS = 6000
# Turns and sections shorter than this ("ok", "thanks!") carry no recallable
# information and are counted as skipped_short instead of being stored.
MIN_TEXT_CHARS = 12
# Epoch values above this are milliseconds, not seconds (year 5138 in seconds).
EPOCH_MILLISECONDS_THRESHOLD = 1e11
MILLISECONDS_PER_SECOND = 1000.0
# Tags are normalised to this many characters at most.
MAX_TAG_CHARS = 64
_TAG_INVALID = re.compile(r"[^\w\-./:]+", re.UNICODE)


class ImportFormatError(ValueError):
    """The input file is not in the format the chosen importer reads."""


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """One unit of source text with its original time and provenance."""

    text: str
    occurred_at: datetime | None
    source_ref: str
    context: str = ""
    tags: tuple[str, ...] = ()
    source_format: str = "auto"
    knowledge_type: str = "fact"
    preserved_identity: str | None = None


@dataclass(slots=True)
class ImportStats:
    """Counters reported as progress and as the final summary."""

    kind: str
    project: str
    dry_run: bool
    units_seen: int = 0
    imported: int = 0
    would_import: int = 0
    deduplicated: int = 0
    already_imported: int = 0
    rejected: int = 0
    skipped_role: int = 0
    skipped_empty: int = 0
    skipped_short: int = 0
    skipped_files: int = 0
    redacted: int = 0
    split_parts: int = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    warnings: list[str] = field(default_factory=list)

    def observe_time(self, moment: datetime | None) -> None:
        if moment is None:
            return
        text = format_utc(moment)
        if self.first_timestamp is None or text < self.first_timestamp:
            self.first_timestamp = text
        if self.last_timestamp is None or text > self.last_timestamp:
            self.last_timestamp = text

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind, "project": self.project, "dry_run": self.dry_run,
            "units_seen": self.units_seen, "imported": self.imported, "would_import": self.would_import,
            "deduplicated": self.deduplicated, "already_imported": self.already_imported,
            "rejected": self.rejected, "skipped_role": self.skipped_role, "skipped_empty": self.skipped_empty,
            "skipped_short": self.skipped_short, "skipped_files": self.skipped_files,
            "redacted": self.redacted, "split_parts": self.split_parts,
            "first_timestamp": self.first_timestamp, "last_timestamp": self.last_timestamp,
            "warnings": list(self.warnings),
        }


def parse_timestamp(value: object, *, strict: bool = False) -> datetime | None:
    """Aware UTC datetime from an epoch number or an ISO-8601 string.

    Zone-less ISO strings are taken as UTC (export formats write UTC). Missing
    values give None; unparseable ones give None, or raise with ``strict``.
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, bool):
            raise TypeError("boolean is not a timestamp")
        if isinstance(value, (int, float)):
            seconds = float(value)
            if not math.isfinite(seconds):
                raise ValueError("non-finite epoch")
            if abs(seconds) > EPOCH_MILLISECONDS_THRESHOLD:
                seconds /= MILLISECONDS_PER_SECOND
            return datetime.fromtimestamp(seconds, UTC)
        if isinstance(value, str):
            text = value.strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None or moment.utcoffset() is None:
                moment = moment.replace(tzinfo=UTC)
            return moment.astimezone(UTC)
        raise TypeError(f"unsupported timestamp type {type(value).__name__}")
    except (TypeError, ValueError, OverflowError, OSError) as error:
        if strict:
            raise ImportFormatError(f"invalid timestamp {value!r}: {error}") from error
        return None


def normalise_tag(raw: object) -> str | None:
    """A tag usable in TAM's tag index, or None when nothing meaningful is left."""
    if not isinstance(raw, str):
        return None
    tag = _TAG_INVALID.sub("-", raw.strip().lstrip("#").lower()).strip("-")
    return tag[:MAX_TAG_CHARS] or None


def split_text(text: str, max_chars: int = MAX_RECORD_CHARS) -> list[str]:
    """Split ``text`` into pieces of at most ``max_chars``, preferring paragraph and line breaks."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    pieces: list[str] = []
    current = ""
    for block in _blocks(text, max_chars):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            pieces.append(current)
        current = block
    if current:
        pieces.append(current)
    return pieces


def _blocks(text: str, max_chars: int) -> list[str]:
    """Paragraphs, with oversized paragraphs broken on lines and then on hard limits."""
    blocks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            blocks.append(paragraph)
            continue
        line_buffer = ""
        for line in paragraph.splitlines():
            while len(line) > max_chars:
                if line_buffer:
                    blocks.append(line_buffer)
                    line_buffer = ""
                blocks.append(line[:max_chars])
                line = line[max_chars:]
            candidate = f"{line_buffer}\n{line}" if line_buffer else line
            if len(candidate) <= max_chars:
                line_buffer = candidate
            else:
                blocks.append(line_buffer)
                line_buffer = line
        if line_buffer:
            blocks.append(line_buffer)
    return blocks


def content_hash(kind: str, project: str, text: str, occurred_at: datetime | None) -> str:
    """Idempotency key of one stored unit: importer, project, exact text and original time."""
    stamp = format_utc(occurred_at) if occurred_at is not None else ""
    digest = hashlib.sha256()
    for part in (kind, project, stamp, text):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()
