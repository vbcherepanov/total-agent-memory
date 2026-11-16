"""Per-tool telemetry: batching, shutdown flush, retention, percentiles, server wiring."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import tool_telemetry
from memory_core.timestamps import format_utc

MIGRATION = ROOT / "migrations" / "041_tool_calls.sql"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
SUBPROCESS_TIMEOUT_SEC = 60


@pytest.fixture(autouse=True)
def close_writers(monkeypatch):
    writers = []
    original = tool_telemetry.ToolTelemetry.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        writers.append(self)

    monkeypatch.setattr(tool_telemetry.ToolTelemetry, "__init__", initialize)
    yield
    for writer in writers:
        writer.close()


def make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATION.read_text(encoding="utf-8"))
    conn.close()
    return path


def rows(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT tool, duration_ms, ok, error_class, args_bytes, result_bytes, profile FROM tool_calls ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path) -> Path:
    return make_db(tmp_path / "telemetry.db")


def telemetry(db: Path, **kwargs) -> tool_telemetry.ToolTelemetry:
    return tool_telemetry.ToolTelemetry(tool_telemetry.sqlite_connector(db), profile="lean",
                                        clock=kwargs.pop("clock", lambda: NOW), **kwargs)


def record(t: tool_telemetry.ToolTelemetry, tool: str = "memory_recall", *, duration_ms: float = 5.0,
           ok: bool = True) -> None:
    t.record(tool, duration_ms=duration_ms, ok=ok, error_class=None if ok else "ValueError",
             args_bytes=10, result_bytes=20)


def test_calls_are_buffered_until_a_batch_is_full(db):
    t = telemetry(db, batch_size=3)
    record(t)
    record(t)
    assert rows(db) == [] and t.pending == 2
    record(t, ok=False)
    t.flush()
    assert t.pending == 0
    assert rows(db) == [("memory_recall", 5.0, 1, None, 10, 20, "lean")] * 2 + [
        ("memory_recall", 5.0, 0, "ValueError", 10, 20, "lean")]


def test_default_batch_size_is_the_named_constant(db):
    t = telemetry(db)
    for _ in range(tool_telemetry.FLUSH_BATCH_SIZE - 1):
        record(t)
    assert rows(db) == []
    record(t)
    t.flush()
    assert len(rows(db)) == tool_telemetry.FLUSH_BATCH_SIZE


def test_explicit_flush_writes_a_partial_batch(db):
    t = telemetry(db)
    record(t)
    assert t.flush() == 1
    assert t.flush() == 0
    assert len(rows(db)) == 1


def test_a_failing_sink_never_raises_and_keeps_the_calls(db, caplog):
    def broken():
        raise sqlite3.OperationalError("disk I/O error")

    t = tool_telemetry.ToolTelemetry(broken, profile="full", batch_size=2)
    with caplog.at_level(logging.ERROR, logger="tool_telemetry"):
        record(t)
        record(t)  # triggers a flush that fails
        t.flush()
    assert t.pending == 2
    assert "flush of 2 calls failed" in caplog.text


def test_the_buffer_of_a_failing_sink_is_bounded(caplog, monkeypatch):
    monkeypatch.setattr(tool_telemetry, "MAX_BUFFERED_CALLS", 5)

    def broken():
        raise sqlite3.OperationalError("locked")

    t = tool_telemetry.ToolTelemetry(broken, profile="full", batch_size=1)
    with caplog.at_level(logging.WARNING, logger="tool_telemetry"):
        for _ in range(8):
            record(t)
    assert t.pending == 5
    assert t.metrics["dropped_calls"] == 3


def test_old_rows_are_pruned_on_flush(db):
    conn = sqlite3.connect(db)
    old = format_utc(NOW - timedelta(days=tool_telemetry.RETENTION_DAYS, minutes=1))
    recent = format_utc(NOW - timedelta(days=tool_telemetry.RETENTION_DAYS - 1))
    conn.executemany("INSERT INTO tool_calls (ts, tool, duration_ms, ok) VALUES (?, ?, 1, 1)",
                     [(old, "old"), (recent, "recent")])
    conn.commit()
    conn.close()
    t = telemetry(db)
    record(t, "fresh")
    t.flush()
    assert [row[0] for row in rows(db)] == ["recent", "fresh"]


def test_pruning_runs_at_most_once_per_interval(db, monkeypatch):
    t = telemetry(db, batch_size=1)
    calls = []
    original = t.prune
    monkeypatch.setattr(t, "prune", lambda conn=None: calls.append(1) or original(conn))
    record(t)
    record(t)
    t.flush()
    assert len(calls) == 1


def test_percentiles_use_nearest_rank():
    values = [float(v) for v in range(1, 101)]
    assert tool_telemetry.percentile(values, 50) == 50.0
    assert tool_telemetry.percentile(values, 95) == 95.0
    assert tool_telemetry.percentile([7.0], 95) == 7.0
    assert tool_telemetry.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0


def test_report_gives_count_percentiles_and_error_rate_per_tool(db):
    t = telemetry(db, batch_size=1000)
    for ms in range(1, 101):
        record(t, "memory_recall", duration_ms=float(ms), ok=ms % 10 != 0)
    for ms in (3.0, 9.0):
        record(t, "memory_save", duration_ms=ms)
    out = t.report()
    assert out["calls"] == 102 and out["window_hours"] == tool_telemetry.DEFAULT_REPORT_WINDOW_HOURS
    assert out["tools"]["memory_recall"] == {"count": 100, "p50_ms": 50.0, "p95_ms": 95.0, "error_rate": 0.1}
    assert out["tools"]["memory_save"] == {"count": 2, "p50_ms": 3.0, "p95_ms": 9.0, "error_rate": 0.0}
    assert list(out["tools"]) == ["memory_recall", "memory_save"]


def test_report_only_covers_the_window(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO tool_calls (ts, tool, duration_ms, ok) VALUES (?, 'stale', 1, 1)",
                 (format_utc(NOW - timedelta(hours=tool_telemetry.DEFAULT_REPORT_WINDOW_HOURS + 1)),))
    conn.commit()
    out = tool_telemetry.report(conn, now=NOW)
    conn.close()
    assert out["tools"] == {} and out["calls"] == 0


def test_payload_bytes_counts_utf8():
    assert tool_telemetry.payload_bytes("ключ") == len("ключ".encode())
    assert tool_telemetry.payload_bytes({"q": "é"}) == len('{"q":"é"}'.encode())


def test_enabled_setting():
    assert tool_telemetry.enabled({})
    assert tool_telemetry.enabled({tool_telemetry.TELEMETRY_ENV: "true"})
    assert not tool_telemetry.enabled({tool_telemetry.TELEMETRY_ENV: "false"})


def test_sigterm_flushes_then_hands_over_to_the_previous_handler(db):
    received = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: received.append(signum))
    try:
        t = telemetry(db)
        t.install_shutdown_hooks()
        record(t)
        signal.raise_signal(signal.SIGTERM)
        assert len(rows(db)) == 1
        assert received == [signal.SIGTERM]
        assert signal.getsignal(signal.SIGTERM) is not t._on_sigterm
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_uninstall_restores_the_previous_handler(db):
    previous = signal.getsignal(signal.SIGTERM)
    t = telemetry(db)
    t.install_shutdown_hooks()
    assert signal.getsignal(signal.SIGTERM) == t._on_sigterm
    t.uninstall_shutdown_hooks()
    assert signal.getsignal(signal.SIGTERM) == previous


CHILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {src!r})
    import tool_telemetry
    t = tool_telemetry.ToolTelemetry(tool_telemetry.sqlite_connector({db!r}), profile="minimal")
    t.install_shutdown_hooks()
    t.record("memory_save", duration_ms=1.5, ok=True, error_class=None, args_bytes=3, result_bytes=4)
    print("ready", flush=True)
    if {wait}:
        time.sleep(60)
""")


def _child(db: Path, wait: bool) -> subprocess.Popen:
    code = CHILD.format(src=str(ROOT / "src"), db=str(db), wait=wait)
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})


def test_normal_exit_flushes_the_partial_batch(db):
    child = _child(db, wait=False)
    child.communicate(timeout=SUBPROCESS_TIMEOUT_SEC)
    assert child.returncode == 0
    assert rows(db) == [("memory_save", 1.5, 1, None, 3, 4, "minimal")]


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is TerminateProcess on Windows: no handler runs")
def test_sigterm_flushes_and_still_terminates_the_process(db):
    child = _child(db, wait=True)
    assert child.stdout.readline().strip() == "ready"
    child.send_signal(signal.SIGTERM)
    child.communicate(timeout=SUBPROCESS_TIMEOUT_SEC)
    assert child.returncode == -signal.SIGTERM
    assert rows(db) == [("memory_save", 1.5, 1, None, 3, 4, "minimal")]


# ── server wiring ──────────────────────────────────────────────────────────

@pytest.fixture
def wired(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store()
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "recall", server.Recall(store))
    monkeypatch.setattr(server, "SID", "sess-telemetry")
    t = tool_telemetry.ToolTelemetry(tool_telemetry.sqlite_connector(store.db_path), profile="full")
    monkeypatch.setattr(server, "TELEMETRY", t)
    yield server, store, t
    store.db.close()


def test_every_tool_call_is_recorded_with_sizes_not_contents(wired):
    server, store, t = wired
    secret = "sk-" + "a" * 40
    asyncio.run(server._call_tool_impl("memory_get", {"ids": [], "note": secret}))
    asyncio.run(server._call_tool_impl("no_such_tool", {}))
    t.flush()
    recorded = store.db.execute(
        "SELECT tool, ok, error_class, args_bytes, result_bytes, profile FROM tool_calls ORDER BY id").fetchall()
    assert [tuple(r)[:3] for r in recorded] == [("memory_get", 1, None), ("no_such_tool", 0, "ValueError")]
    assert recorded[0]["args_bytes"] > 0 and recorded[0]["result_bytes"] > 0
    dump = "\n".join(str(tuple(r)) for r in store.db.execute("SELECT * FROM tool_calls"))
    assert secret not in dump and "aaaa" not in dump


def test_a_raising_tool_records_its_error_class(wired, monkeypatch):
    server, store, t = wired

    async def boom(name, args):
        raise KeyError("query")

    monkeypatch.setattr(server, "_do", boom)
    _, is_error = asyncio.run(server._call_tool_impl("memory_recall", {}))
    assert is_error
    t.flush()
    assert tuple(store.db.execute("SELECT ok, error_class FROM tool_calls").fetchone()) == (0, "KeyError")


def test_broken_telemetry_does_not_break_the_tool_call(wired, monkeypatch):
    server, _, t = wired
    monkeypatch.setattr(t, "_connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("gone")))
    monkeypatch.setattr(t, "batch_size", 1)
    content, is_error = asyncio.run(server._call_tool_impl("memory_get", {"ids": []}))
    assert not is_error and json.loads(content[0].text) is not None


def test_perf_report_includes_per_tool_stats(wired):
    server, _, _ = wired
    for _ in range(3):
        asyncio.run(server._call_tool_impl("memory_get", {"ids": []}))
    report = json.loads(asyncio.run(server._do("memory_perf_report", {})))
    stats = report["tool_calls"]
    assert stats["enabled"] is True
    assert stats["tools"]["memory_get"]["count"] == 3
    assert set(stats["tools"]["memory_get"]) == {"count", "p50_ms", "p95_ms", "error_rate"}


def test_perf_report_reads_the_table_when_telemetry_is_off(wired, monkeypatch):
    server, _, t = wired
    asyncio.run(server._call_tool_impl("memory_get", {"ids": []}))
    t.flush()
    monkeypatch.setattr(server, "TELEMETRY", None)
    stats = json.loads(asyncio.run(server._do("memory_perf_report", {})))["tool_calls"]
    assert stats["enabled"] is False and stats["tools"]["memory_get"]["count"] == 1


def test_install_respects_the_setting(wired, monkeypatch):
    server, store, _ = wired
    monkeypatch.setattr(server, "TELEMETRY", None)
    monkeypatch.setenv(tool_telemetry.TELEMETRY_ENV, "false")
    server._install_telemetry(store.db_path)
    assert server.TELEMETRY is None
    monkeypatch.setenv(tool_telemetry.TELEMETRY_ENV, "true")
    monkeypatch.setenv("TAM_TOOL_PROFILE", "minimal")
    server._install_telemetry(store.db_path)
    try:
        assert server.TELEMETRY is not None and server.TELEMETRY.profile == "minimal"
        first = server.TELEMETRY
        server._install_telemetry(store.db_path)
        assert server.TELEMETRY is first
    finally:
        server.TELEMETRY.uninstall_shutdown_hooks()


def test_store_migrations_create_the_table(wired):
    _, store, _ = wired
    columns = [row[1] for row in store.db.execute("PRAGMA table_info(tool_calls)")]
    assert columns == ["id", "ts", "tool", "duration_ms", "ok", "error_class", "args_bytes", "result_bytes",
                       "profile"]
    assert store.db.execute("SELECT 1 FROM migrations WHERE version = '041'").fetchone() is not None


def test_settings_help_matches_the_retention():
    from settings_catalog import LOCAL_CATALOG

    spec = next(s for s in LOCAL_CATALOG if s.key == tool_telemetry.TELEMETRY_ENV)
    assert f"kept {tool_telemetry.RETENTION_DAYS} days" in spec.help


def test_blocked_writer_does_not_block_record(db):
    entered = threading.Event()
    release = threading.Event()
    connect = tool_telemetry.sqlite_connector(db)

    def blocked_connect():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test writer was not released")
        return connect()

    t = tool_telemetry.ToolTelemetry(blocked_connect, profile="full", batch_size=1)
    try:
        record(t)
        assert entered.wait(2)
        completed = threading.Event()
        caller = threading.Thread(target=lambda: (record(t), completed.set()))
        caller.start()
        assert completed.wait(1), "record waited for the database writer"
        caller.join(1)
        assert t.pending == 2
    finally:
        release.set()
        t.close()
    assert len(rows(db)) == 2


def test_sqlite_write_lock_does_not_delay_batch_submission(db):
    blocker = sqlite3.connect(db)
    blocker.execute("BEGIN IMMEDIATE")
    t = telemetry(db)
    try:
        started = time.monotonic()
        for _ in range(tool_telemetry.FLUSH_BATCH_SIZE):
            record(t)
        assert time.monotonic() - started < tool_telemetry.SQLITE_BUSY_TIMEOUT_SEC
    finally:
        blocker.rollback()
        blocker.close()
        t.close()
    assert len(rows(db)) == tool_telemetry.FLUSH_BATCH_SIZE


def test_partial_batch_flushes_without_another_call(db):
    t = telemetry(db, flush_interval=0.01)
    record(t)
    deadline = time.monotonic() + 2
    while not rows(db) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(rows(db)) == 1


def test_shutdown_is_bounded_and_recovers_after_sink_unblocks(db):
    entered = threading.Event()
    release = threading.Event()
    connect = tool_telemetry.sqlite_connector(db)

    def blocked_connect():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test writer was not released")
        return connect()

    t = tool_telemetry.ToolTelemetry(blocked_connect, profile="full", batch_size=1, shutdown_timeout=0.01)
    try:
        record(t)
        assert entered.wait(2)
        started = time.monotonic()
        t.close()
        assert time.monotonic() - started < 1
        assert t.metrics["shutdown_timeouts"] == 1
        record(t)
        assert t.metrics["dropped_calls"] == 1
    finally:
        release.set()
        t._worker.join(2)
    assert len(rows(db)) == 1
