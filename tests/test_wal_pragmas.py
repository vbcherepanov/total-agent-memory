"""Tests for WAL bloat / write-lock fixes in Store.__init__ (journal_size_limit,
busy_timeout, opportunistic startup checkpoint)."""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def server_mod(monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    return server


def _collect_log(monkeypatch, server_mod):
    lines: list[str] = []
    monkeypatch.setattr(server_mod, "LOG", lines.append)
    return lines


def test_journal_size_limit_and_busy_timeout_applied(server_mod):
    store = server_mod.Store()
    try:
        limit = store.db.execute("PRAGMA journal_size_limit").fetchone()[0]
        timeout = store.db.execute("PRAGMA busy_timeout").fetchone()[0]
        assert limit == 50331648
        assert timeout == 15000
    finally:
        store.db.close()


def test_store_init_survives_busy_startup_checkpoint(server_mod, tmp_path, monkeypatch):
    warmup = server_mod.Store()
    warmup.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) "
        "VALUES ('sess-wal-test', '2026-04-19T00:00:00Z', 'myproj', 'open')"
    )
    warmup.db.commit()
    warmup.db.close()

    db_path = str(tmp_path / "memory.db")
    blocker = sqlite3.connect(db_path)
    blocker.execute("BEGIN")
    blocker.execute("SELECT count(*) FROM sessions").fetchone()

    sneak = sqlite3.connect(db_path)
    sneak.execute("PRAGMA busy_timeout=2000")
    sneak.execute(
        "INSERT INTO sessions (id, started_at, project, status) "
        "VALUES ('sess-wal-test-2', '2026-04-19T00:00:01Z', 'myproj', 'open')"
    )
    sneak.commit()
    sneak.close()

    lines = _collect_log(monkeypatch, server_mod)
    try:
        started = time.monotonic()
        store = server_mod.Store()
        elapsed = time.monotonic() - started
        try:
            assert elapsed < 5
            assert any(line.startswith("WAL checkpoint busy:") for line in lines), lines
            ids = {row[0] for row in store.db.execute("SELECT id FROM sessions")}
            assert {"sess-wal-test", "sess-wal-test-2"} <= ids
            timeout = store.db.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout == 15000
        finally:
            store.db.close()
    finally:
        blocker.close()


def test_store_init_truncates_wal_when_no_reader_holds_it(server_mod, tmp_path, monkeypatch):
    writer = server_mod.Store()
    writer.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) "
        "VALUES ('sess-wal-ok', '2026-04-19T00:00:00Z', 'myproj', 'open')"
    )
    writer.db.commit()
    wal = tmp_path / "memory.db-wal"
    assert wal.stat().st_size > 0

    lines = _collect_log(monkeypatch, server_mod)
    store = server_mod.Store()
    try:
        assert any(line.startswith("WAL checkpoint: truncated") for line in lines), lines
        assert wal.stat().st_size == 0
    finally:
        store.db.close()
        writer.db.close()
