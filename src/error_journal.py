"""Journal of swallowed best-effort failures (table ``error_log``, migration 039).

Many write-path stages are deliberately best-effort: a failed graph link or a
missing vocabulary file must not fail the save. Those failures used to go to
stderr only, which nobody reads on a hosted install. The journal keeps them in
the database for ``ERROR_RETENTION_DAYS`` and ``memory_stats`` reports recent
counts per component.

Guarantees:
  * ``log_error`` never raises; a journal failure goes to stderr instead.
  * It never commits a caller's transaction: while the connection is inside a
    transaction the entry waits in a bounded in-memory buffer and is written by
    the next call (or ``flush``) that finds the connection idle. Entries logged
    from a thread other than the journal's creator are buffered the same way.
  * Floods are rate limited per ``(component, exc_type)``: at most
    ``RATE_LIMIT_MAX_PER_WINDOW`` rows per ``RATE_LIMIT_WINDOW_SECONDS``; the rest
    are counted and written as one rollup row (``occurrences`` > 1) when the
    window closes.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from memory_core.timestamps import format_utc

ERROR_RETENTION_DAYS = 14
RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_MAX_PER_WINDOW = 10
PURGE_INTERVAL_SECONDS = 3600.0
BUFFER_MAX_ENTRIES = 500
MESSAGE_MAX_CHARS = 1000
TRACE_HEAD_MAX_CHARS = 2000
TRACE_HEAD_FRAMES = 6
COMPONENT_MAX_CHARS = 100
DEFAULT_SUMMARY_HOURS = 24
SUPPRESSED_MESSAGE = "rate limited: {count} more occurrence(s) of this error"


class _Connection(Protocol):
    @property
    def in_transaction(self) -> bool: ...

    def execute(self, sql: str, parameters: Any = (), /) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@dataclass(frozen=True)
class JournalEntry:
    ts: str
    component: str
    message: str
    exc_type: str
    trace_head: str | None
    occurrences: int = 1


@dataclass
class _Window:
    started: float
    written: int = 0
    suppressed: int = 0
    last_message: str = ""


def _stderr(message: str) -> None:
    sys.stderr.write(f"[error-journal] {message}\n")


def _trace_head(exc: BaseException | None) -> str | None:
    if exc is None or exc.__traceback__ is None:
        return None
    frames = traceback.format_tb(exc.__traceback__)[-TRACE_HEAD_FRAMES:]
    return "".join(frames)[-TRACE_HEAD_MAX_CHARS:] or None


class ErrorJournal:
    """Writes best-effort failures to ``error_log`` over an injected connection."""

    def __init__(self, db: _Connection, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.db = db
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._windows: dict[tuple[str, str], _Window] = {}
        self._buffer: deque[JournalEntry] = deque(maxlen=BUFFER_MAX_ENTRIES)
        self._last_purge = float("-inf")
        # Connections are not shared across threads for writes: entries
        # logged from another thread wait for the owner's next flush.
        self._owner = threading.get_ident()

    def _now_text(self) -> str:
        return format_utc(datetime.fromtimestamp(self._clock(), tz=UTC))

    def log_error(self, component: str, exc: BaseException | None, message: str) -> None:
        """Record one failure. Never raises."""
        try:
            entry = self._admit(component, exc, message)
            if entry is not None:
                self._buffer.append(entry)
            self.flush()
        except Exception as error:  # noqa: BLE001 — the journal must never break its caller
            _stderr(f"log_error failed for {component!r}: {type(error).__name__}: {error}")

    def _admit(self, component: str, exc: BaseException | None, message: str) -> JournalEntry | None:
        component = (component or "unknown")[:COMPONENT_MAX_CHARS]
        exc_type = type(exc).__name__ if exc is not None else "None"
        text = (message or "").strip()
        if exc is not None and str(exc):
            text = f"{text}: {exc}" if text else str(exc)
        text = text[:MESSAGE_MAX_CHARS] or exc_type
        key = (component, exc_type)
        now = self._monotonic()
        with self._lock:
            window = self._windows.get(key)
            if window is None or now - window.started >= RATE_LIMIT_WINDOW_SECONDS:
                if window is not None and window.suppressed:
                    self._buffer.append(self._rollup(component, exc_type, window))
                window = _Window(started=now)
                self._windows[key] = window
            if window.written >= RATE_LIMIT_MAX_PER_WINDOW:
                window.suppressed += 1
                window.last_message = text
                return None
            window.written += 1
        return JournalEntry(ts=self._now_text(), component=component, message=text,
                            exc_type=exc_type, trace_head=_trace_head(exc))

    def _rollup(self, component: str, exc_type: str, window: _Window) -> JournalEntry:
        message = SUPPRESSED_MESSAGE.format(count=window.suppressed)
        if window.last_message:
            message = f"{message}; last: {window.last_message}"[:MESSAGE_MAX_CHARS]
        return JournalEntry(ts=self._now_text(), component=component, message=message,
                            exc_type=exc_type, trace_head=None, occurrences=window.suppressed)

    def _close_expired_windows(self) -> None:
        now = self._monotonic()
        with self._lock:
            for key, window in list(self._windows.items()):
                if now - window.started >= RATE_LIMIT_WINDOW_SECONDS:
                    if window.suppressed:
                        self._buffer.append(self._rollup(key[0], key[1], window))
                    del self._windows[key]

    def flush(self) -> int:
        """Write buffered entries when the connection is idle; returns rows written. Never raises."""
        try:
            self._close_expired_windows()
            if not self._buffer or threading.get_ident() != self._owner or self.db.in_transaction:
                return 0
            entries = list(self._buffer)
            self._buffer.clear()
            try:
                for entry in entries:
                    self.db.execute(
                        "INSERT INTO error_log (ts, component, message, exc_type, trace_head, occurrences) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (entry.ts, entry.component, entry.message, entry.exc_type,
                         entry.trace_head, entry.occurrences))
                self._maybe_purge()
                self.db.commit()
            except Exception as error:  # noqa: BLE001 — table missing / locked: keep stderr trail
                self._safe_rollback()
                _stderr(f"could not write {len(entries)} entr(y/ies): {type(error).__name__}: {error}")
                return 0
            return len(entries)
        except Exception as error:  # noqa: BLE001
            _stderr(f"flush failed: {type(error).__name__}: {error}")
            return 0

    def _safe_rollback(self) -> None:
        try:
            self.db.rollback()
        except Exception as error:  # noqa: BLE001
            _stderr(f"rollback failed: {type(error).__name__}: {error}")

    def _maybe_purge(self) -> None:
        now = self._monotonic()
        if now - self._last_purge < PURGE_INTERVAL_SECONDS:
            return
        self._last_purge = now
        self.purge()

    def purge(self) -> int:
        """Delete rows older than the retention window (caller commits)."""
        cutoff = format_utc(datetime.fromtimestamp(self._clock(), tz=UTC) - timedelta(days=ERROR_RETENTION_DAYS))
        cursor = self.db.execute("DELETE FROM error_log WHERE ts < ?", (cutoff,))
        return int(cursor.rowcount or 0)

    def recent_counts(self, hours: int = DEFAULT_SUMMARY_HOURS) -> dict[str, Any]:
        """Occurrences per component over the last ``hours``; empty counts when the table is missing."""
        self.flush()
        cutoff = format_utc(datetime.fromtimestamp(self._clock(), tz=UTC) - timedelta(hours=hours))
        was_idle = not self.db.in_transaction
        try:
            rows = self.db.execute(
                "SELECT component, SUM(occurrences) FROM error_log WHERE ts >= ? "
                "GROUP BY component ORDER BY component", (cutoff,)).fetchall()
        except sqlite3.Error as error:
            if was_idle and self.db.in_transaction:
                self._safe_rollback()
            return {"window_hours": hours, "total": 0, "by_component": {}, "error": str(error)}
        by_component = {row[0]: int(row[1] or 0) for row in rows}
        return {"window_hours": hours, "total": sum(by_component.values()),
                "by_component": by_component, "buffered": len(self._buffer)}


_installed: ErrorJournal | None = None


def install(journal: ErrorJournal | None) -> None:
    """Make ``journal`` the process default used by the module-level ``log_error``."""
    global _installed
    _installed = journal


def log_error(component: str, exc: BaseException | None, message: str) -> None:
    """Record a failure in the installed journal (stderr when none is installed). Never raises."""
    journal = _installed
    if journal is None:
        _stderr(f"{component}: {message}: {type(exc).__name__ if exc else 'None'}: {exc}")
        return
    journal.log_error(component, exc, message)
