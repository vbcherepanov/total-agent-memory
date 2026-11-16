"""Per-tool call telemetry: how often each MCP tool runs, how long it takes, how often it fails.

Rows land in ``tool_calls`` (migration 041). Calls are buffered in memory and
written in batches of ``FLUSH_BATCH_SIZE``, plus once more at interpreter exit
and on SIGTERM, so a tool call never waits on a write. Rows older than
``RETENTION_DAYS`` are pruned at most once per ``PRUNE_INTERVAL_SEC``.

Only sizes are stored, never argument or result contents: a call that carried a
secret leaves nothing behind but its byte count.

Telemetry is best-effort by contract: every failure is logged and swallowed
here, never raised into the tool call that produced the sample.
``TAM_TOOL_TELEMETRY=false`` disables it.
"""

from __future__ import annotations

import atexit
import json
import logging
import math
import signal
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import astuple, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any, Final, Protocol

from memory_core.timestamps import format_utc
from memory_core.telemetry import Counter, LATENCY_BUCKETS_MS

LOGGER = logging.getLogger(__name__)

TELEMETRY_ENV: Final = "TAM_TOOL_TELEMETRY"
FLUSH_BATCH_SIZE: Final = 25
# A sink that keeps failing must not grow the buffer without bound.
MAX_BUFFERED_CALLS: Final = 1000
RETENTION_DAYS: Final = 7
PRUNE_INTERVAL_SEC: Final = 3600
DEFAULT_REPORT_WINDOW_HOURS: Final = 24
SQLITE_BUSY_TIMEOUT_SEC: Final = 0.25
FLUSH_INTERVAL_SEC: Final = 1.0
SHUTDOWN_TIMEOUT_SEC: Final = 1.0
MS_PER_SEC: Final = 1000.0
PERCENTILES: Final = (50, 95)
_FALSE: Final = frozenset({"false", "0", "no", "off"})

_INSERT: Final = ("INSERT INTO tool_calls (ts, tool, duration_ms, ok, error_class, args_bytes, result_bytes, profile) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?)")


class Connection(Protocol):
    def execute(self, sql: str, parameters: Iterable[Any] = ..., /) -> Any: ...
    def executemany(self, sql: str, parameters: Iterable[Iterable[Any]], /) -> Any: ...
    def commit(self) -> None: ...
    def close(self) -> None: ...


class _PruneFailed(RuntimeError):
    """Retention pruning failed after the batch was committed; the batch must not be re-buffered."""


@dataclass(frozen=True)
class ToolCall:
    ts: str
    tool: str
    duration_ms: float
    ok: int
    error_class: str | None
    args_bytes: int
    result_bytes: int
    profile: str


def enabled(environ: Mapping[str, str]) -> bool:
    return environ.get(TELEMETRY_ENV, "").strip().lower() not in _FALSE


def payload_bytes(value: Any) -> int:
    """UTF-8 size of `value` as compact JSON; the measure stored instead of the content."""
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def sqlite_connector(db_path: Path) -> Callable[[], sqlite3.Connection]:
    """A factory of short-lived connections, so flushes never share the store's transaction."""
    def connect() -> sqlite3.Connection:
        return sqlite3.connect(str(db_path), timeout=SQLITE_BUSY_TIMEOUT_SEC)
    return connect


def percentile(sorted_values: list[float], pct: int) -> float:
    """Nearest-rank percentile of an ascending, non-empty list."""
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def report(conn: Connection, *, window_hours: float = DEFAULT_REPORT_WINDOW_HOURS,
           now: datetime | None = None) -> dict[str, Any]:
    """Per-tool count, p50/p95 latency and error rate over the last `window_hours`."""
    moment = now or datetime.now(UTC)
    since = format_utc(moment - timedelta(hours=window_hours))
    rows = conn.execute(
        "SELECT tool, duration_ms, ok FROM tool_calls WHERE ts >= ? ORDER BY tool, duration_ms", (since,)
    ).fetchall()
    grouped: dict[str, list[tuple[float, int]]] = {}
    for tool, duration_ms, ok in rows:
        grouped.setdefault(tool, []).append((float(duration_ms), int(ok)))
    tools = {}
    for tool, samples in sorted(grouped.items(), key=lambda item: -len(item[1])):
        durations = sorted(duration for duration, _ in samples)
        errors = sum(1 for _, ok in samples if not ok)
        tools[tool] = {
            "count": len(samples),
            **{f"p{pct}_ms": round(percentile(durations, pct), 2) for pct in PERCENTILES},
            "error_rate": round(errors / len(samples), 4),
        }
    return {"window_hours": window_hours, "since": since, "calls": len(rows), "tools": tools}


class ToolTelemetry:
    """Buffered writer of ``tool_calls`` rows."""

    def __init__(self, connect: Callable[[], Connection], *, profile: str,
                 batch_size: int = FLUSH_BATCH_SIZE, retention_days: int = RETENTION_DAYS,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 flush_interval: float = FLUSH_INTERVAL_SEC,
                 shutdown_timeout: float = SHUTDOWN_TIMEOUT_SEC):
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if flush_interval <= 0 or shutdown_timeout <= 0:
            raise ValueError("Telemetry timeouts must be positive")
        self._connect = connect
        self.profile = profile
        self.batch_size = batch_size
        self.retention_days = retention_days
        self._clock = clock
        self._buffer: list[ToolCall] = []
        # Re-entrant: the SIGTERM handler runs on the main thread and may interrupt record().
        self._lock = threading.RLock()
        self._last_prune: float | None = None
        self._previous_sigterm: Any = None
        self._hooks_installed = False
        self._flush_interval = flush_interval
        self._shutdown_timeout = shutdown_timeout
        self._write_lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None
        self._inflight = 0
        self._metrics = Counter()

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._buffer) + self._inflight

    @property
    def metrics(self) -> dict[str, float]:
        return self._metrics.snapshot()

    def record(self, tool: str, *, duration_ms: float, ok: bool, error_class: str | None,
               args_bytes: int, result_bytes: int) -> None:
        """Enqueue one call without waiting for the database writer."""
        try:
            call = ToolCall(format_utc(self._clock()), tool, round(duration_ms, 3), int(ok), error_class,
                            args_bytes, result_bytes, self.profile)
            with self._lock:
                if self._stopping.is_set() or len(self._buffer) + self._inflight >= MAX_BUFFERED_CALLS:
                    self._metrics.bump("dropped_calls")
                    return
                self._buffer.append(call)
                self._metrics.bump("enqueued_calls")
                if self._worker is None:
                    self._worker = threading.Thread(target=self._run_writer, name="tam-telemetry", daemon=True)
                    self._worker.start()
                full = len(self._buffer) >= self.batch_size
            if full:
                self._wake.set()
        except Exception:
            LOGGER.exception("tool telemetry: could not record a call to %s", tool)

    def flush(self) -> int:
        """Write buffered calls; returns how many were written. Never raises."""
        with self._write_lock:
            with self._lock:
                batch, self._buffer = self._buffer, []
                self._inflight = len(batch)
            if not batch:
                return 0
            started = time.monotonic()
            try:
                conn = self._connect()
                try:
                    conn.executemany(_INSERT, [astuple(call) for call in batch])
                    conn.commit()
                    self._prune_if_due(conn)
                finally:
                    conn.close()
            except _PruneFailed:
                LOGGER.exception("tool telemetry: pruning old calls failed; the batch itself was written")
            except Exception:
                LOGGER.exception("tool telemetry: flush of %d calls failed; keeping them for the next one",
                                 len(batch))
                self._metrics.bump("flush_failures")
                self._metrics.bump("retried_calls", len(batch))
                with self._lock:
                    self._buffer = batch + self._buffer
                    self._inflight = 0
                return 0
            finally:
                duration = (time.monotonic() - started) * MS_PER_SEC
                self._metrics.bump("flush_ms", duration)
                self._metrics.bump("flush_ms_count")
                for bound in LATENCY_BUCKETS_MS:
                    if duration <= bound:
                        self._metrics.bump(f"flush_ms_bucket_le_{bound}")
                self._metrics.bump("flush_ms_bucket_le_inf")
            with self._lock:
                self._inflight = 0
            self._metrics.bump("written_calls", len(batch))
            return len(batch)

    def _run_writer(self) -> None:
        while True:
            self._wake.wait(self._flush_interval)
            self._wake.clear()
            self.flush()
            if self._stopping.is_set():
                self.flush()
                return

    def close(self) -> None:
        self._stopping.set()
        self._wake.set()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(self._shutdown_timeout)
            if worker.is_alive():
                self._metrics.bump("shutdown_timeouts")
                LOGGER.warning("tool telemetry: shutdown drain timed out", extra={"pending": self.pending})

    def prune(self, conn: Connection | None = None) -> int:
        """Delete rows older than the retention window; returns the rows removed."""
        cutoff = format_utc(self._clock() - timedelta(days=self.retention_days))
        own = conn is None
        target = self._connect() if own else conn
        try:
            removed = target.execute("DELETE FROM tool_calls WHERE ts < ?", (cutoff,)).rowcount
            target.commit()
        finally:
            if own:
                target.close()
        self._last_prune = time.monotonic()
        return max(removed, 0)

    def report(self, window_hours: float = DEFAULT_REPORT_WINDOW_HOURS) -> dict[str, Any]:
        """Flush, then summarise the window from the table (calls from every process included)."""
        self.flush()
        conn = self._connect()
        try:
            return {**report(conn, window_hours=window_hours, now=self._clock()), "writer": self.metrics}
        finally:
            conn.close()

    def _prune_if_due(self, conn: Connection) -> None:
        if self._last_prune is not None and time.monotonic() - self._last_prune < PRUNE_INTERVAL_SEC:
            return
        try:
            removed = self.prune(conn)
        except Exception as error:
            raise _PruneFailed(str(error)) from error
        if removed:
            LOGGER.info("tool telemetry: pruned %d calls older than %d days", removed, self.retention_days)

    def install_shutdown_hooks(self) -> None:
        """Flush at interpreter exit and on SIGTERM (then hand the signal to the previous handler)."""
        if self._hooks_installed:
            return
        atexit.register(self.close)
        try:
            self._previous_sigterm = signal.signal(signal.SIGTERM, self._on_sigterm)
        except ValueError:
            # signal.signal only works on the main thread; atexit still covers a normal exit.
            LOGGER.warning("tool telemetry: not on the main thread, SIGTERM flush not installed")
        self._hooks_installed = True

    def uninstall_shutdown_hooks(self) -> None:
        if not self._hooks_installed:
            return
        atexit.unregister(self.close)
        self.close()
        if signal.getsignal(signal.SIGTERM) == self._on_sigterm:
            signal.signal(signal.SIGTERM, self._previous_sigterm or signal.SIG_DFL)
        self._hooks_installed = False

    def _on_sigterm(self, signum: int, frame: FrameType | None) -> None:
        self.close()
        previous = self._previous_sigterm
        signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)
        self._hooks_installed = False
        atexit.unregister(self.close)
        if callable(previous):
            previous(signum, frame)
        else:
            signal.raise_signal(signum)
