"""memory_delete(hard=true): the record, its earlier versions and every derived copy leave the disk."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests.pg_store_support import store_backend  # noqa: F401 — fixture

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

NEEDLE = "Zanzibarquokka"
OLD = f"Customer {NEEDLE} asked to cancel the enterprise contract in March"
NEW = f"Customer {NEEDLE} renewed the enterprise contract for two years"


@pytest.fixture
def live_server(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    for sub in ("raw", "blobs", "chroma"):
        (tmp_path / sub).mkdir(exist_ok=True)
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store(database=store_backend)
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "recall", server.Recall(store))
    monkeypatch.setattr(server, "SID", "sess-erase")
    monkeypatch.setattr(server, "BRANCH", "")
    monkeypatch.setattr(server, "_v5_modules", {})
    store.db.execute("INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, ?, ?)",
                     (server.SID, "2026-09-28T00:00:00Z", "p", "open"))
    store.db.commit()
    yield server, store, tmp_path
    store.db.close()


def call(server, tool, **args):
    content, is_error = asyncio.run(server._call_tool_impl(tool, args))
    return json.loads(content[0].text) if content[0].text.startswith("{") else content[0].text, is_error


def files_containing(root: Path, needle: str) -> list[str]:
    """Case-insensitive: FTS5 stores tokens lower-cased."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")
                  if path.is_file() and needle.lower().encode() in path.read_bytes().lower())


def test_hard_delete_rebuilds_the_database_so_earlier_writes_leave_no_text(live_server):
    """An ordinary update or delete leaves the old bytes in the page's free space; nothing writes after the erasure here to cover them."""
    server, store, root = live_server
    if store.is_postgres:
        pytest.skip("personal memory is SQLite")
    first, _ = call(server, "memory_save", type="fact", project="p", content=OLD)
    call(server, "memory_save", type="fact", project="p", content="Unrelated note about CI caching")
    updated, _ = call(server, "memory_update", id=first["id"], new_content=NEW, reason="renewal")
    secure_delete = store.db.execute("PRAGMA secure_delete").fetchone()[0]

    result, is_error = call(server, "memory_delete", id=updated["new_id"], hard=True)

    assert not is_error, result
    assert "sqlite_not_compacted" not in result
    assert store.db.execute("PRAGMA secure_delete").fetchone()[0] == secure_delete
    assert files_containing(root, NEEDLE) == []
    assert [row[0] for row in store.db.execute("SELECT content FROM knowledge")] == ["Unrelated note about CI caching"]


def test_compact_sqlite_reports_a_database_another_connection_is_writing(tmp_path):
    import sqlite3

    import erasure

    path = tmp_path / "busy.db"
    db = sqlite3.connect(path, timeout=0.1)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (x)")
    db.commit()
    writer = sqlite3.connect(path, timeout=0.1)
    writer.execute("BEGIN IMMEDIATE")
    try:
        assert "locked" in erasure.compact_sqlite(db)
    finally:
        writer.rollback()
        writer.close()
    assert erasure.compact_sqlite(db) is None
    db.close()


def test_hard_delete_erases_record_versions_and_every_copy(live_server):
    server, store, root = live_server
    if store.is_postgres:
        pytest.skip("personal memory is SQLite; the Postgres refusal is covered below")
    first, _ = call(server, "memory_save", type="fact", project="p", content=OLD)
    call(server, "memory_save", type="fact", project="p", content="Unrelated note about CI caching")
    updated, _ = call(server, "memory_update", id=first["id"], new_content=NEW, reason="renewal")
    call(server, "memory_recall", query="enterprise contract cancellation", project="p")
    successor, _ = call(server, "memory_save", type="fact", project="p",
                        content="Enterprise renewals are tracked in the CRM pipeline")
    store.db.execute("UPDATE knowledge SET context=? WHERE id=?", (f"Was: {NEW[:200]}", successor["id"]))
    store.db.commit()
    chroma = store._chroma_collection_for("text")
    if chroma is not None:
        vector = [0.1] * 384
        chroma.upsert(ids=[str(first["id"]), str(updated["new_id"])], embeddings=[vector, vector],
                      documents=[OLD, NEW])

    result, is_error = call(server, "memory_delete", id=updated["new_id"], hard=True)

    assert not is_error, result
    assert sorted(result["erased"]) == sorted([first["id"], updated["new_id"]])
    assert result["raw_logs_rewritten"] >= 1
    assert store.db.execute("SELECT COUNT(*) FROM knowledge WHERE id IN (?, ?)",
                            (first["id"], updated["new_id"])).fetchone()[0] == 0
    assert store.db.execute("SELECT context FROM knowledge WHERE id=?", (successor["id"],)).fetchone()[0] \
        == "Was: [erased]"
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert files_containing(root, NEEDLE) == []
    found, _ = call(server, "memory_recall", query=NEEDLE, project="p")
    assert NEEDLE not in json.dumps(found["results"])
    assert store.db.execute("SELECT COUNT(*) FROM knowledge WHERE status='active'").fetchone()[0] == 2


def test_soft_delete_is_still_the_default(live_server):
    server, store, _ = live_server
    saved, _ = call(server, "memory_save", type="fact", project="p", content=OLD)
    result, _ = call(server, "memory_delete", id=saved["id"])
    assert result["deleted"] is True and "hard" not in result
    assert store.db.execute("SELECT status FROM knowledge WHERE id=?", (saved["id"],)).fetchone()[0] == "deleted"


def test_hard_delete_of_a_missing_record_reports_it(live_server):
    server, store, _ = live_server
    if store.is_postgres:
        pytest.skip("personal memory is SQLite")
    result, is_error = call(server, "memory_delete", id=987654, hard=True)
    assert not is_error
    assert result == {"error": "Record not found", "id": 987654}


def test_hard_delete_refuses_a_postgres_store(live_server):
    server, store, _ = live_server
    if not store.is_postgres:
        pytest.skip("Postgres-only refusal")
    saved, _ = call(server, "memory_save", type="fact", project="p", content=OLD)
    result, is_error = call(server, "memory_delete", id=saved["id"], hard=True)
    assert is_error
    assert "SQLite" in result


def test_scrub_hnsw_tail_zeroes_only_the_unused_slots(tmp_path):
    import struct

    from erasure import HNSW_HEADER, scrub_hnsw_tail

    element, capacity, count = 16, 10, 3
    segment = tmp_path / "segment-uuid"
    segment.mkdir()
    (segment / "header.bin").write_bytes(HNSW_HEADER.pack(1, 0, capacity, count, element, 8, 4) + bytes(48))
    live = b"L" * (count * element)
    (segment / "data_level0.bin").write_bytes(live + NEEDLE.encode().ljust((capacity - count) * element, b"x"))

    assert scrub_hnsw_tail(tmp_path) == (1, [])
    data = (segment / "data_level0.bin").read_bytes()
    assert data[:len(live)] == live and not any(data[len(live):])
    assert scrub_hnsw_tail(tmp_path) == (0, [])

    (segment / "header.bin").write_bytes(struct.pack("<I", 7) + bytes(96))
    (segment / "data_level0.bin").write_bytes(NEEDLE.encode() * 4)
    assert scrub_hnsw_tail(tmp_path) == (0, [])
    assert NEEDLE.encode() in (segment / "data_level0.bin").read_bytes()


def test_scrub_hnsw_tail_reports_a_locked_file_instead_of_raising(tmp_path, monkeypatch):
    from erasure import HNSW_HEADER, scrub_hnsw_tail

    segment = tmp_path / "segment-uuid"
    segment.mkdir()
    (segment / "header.bin").write_bytes(HNSW_HEADER.pack(1, 0, 4, 1, 16, 8, 4))
    vectors = segment / "data_level0.bin"
    vectors.write_bytes(b"\0" * 16 + b"secret".ljust(48, b"x"))
    real_open = Path.open

    def locked(self, mode="r", *args, **kwargs):
        if self.name == "data_level0.bin" and "+" in mode:
            raise PermissionError("sharing violation")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked)
    assert scrub_hnsw_tail(tmp_path) == (0, [str(vectors)])
    assert b"secret" in vectors.read_bytes()


def test_chroma_writes_do_not_refill_a_scrubbed_tail(tmp_path):
    import struct

    chromadb = pytest.importorskip("chromadb")
    from erasure import scrub_hnsw_tail

    client = chromadb.PersistentClient(path=str(tmp_path))
    collection = client.get_or_create_collection(
        "scrub-check", configuration={"hnsw": {"sync_threshold": 2, "batch_size": 2}})
    vectors_of = lambda lo, hi: dict(ids=[str(i) for i in range(lo, hi)],  # noqa: E731
                                     embeddings=[[0.01 * (i + 1)] * 8 for i in range(lo, hi)])
    collection.upsert(**vectors_of(0, 6))
    data_file = next(tmp_path.glob("*/data_level0.bin"))
    header = struct.unpack("<IQQQQ", (data_file.parent / "header.bin").read_bytes()[:36])
    live = header[3] * header[4]
    with data_file.open("r+b") as handle:
        handle.seek(live)
        handle.write(b"\xab" * (data_file.stat().st_size - live))
    scrubbed, failed = scrub_hnsw_tail(tmp_path)
    assert (scrubbed, failed) == (1, [])
    collection.upsert(**vectors_of(6, 9))
    after_flush = data_file.read_bytes()
    assert b"\xab" * 16 not in after_flush
    collection.upsert(**vectors_of(9, 150))
    assert b"\xab" * 16 not in data_file.read_bytes()


HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from erasure import chroma_write_lock
with chroma_write_lock(Path(sys.argv[2])) as taken:
    print("held" if taken else "missed", flush=True)
    sys.stdin.read()
"""


def _segment_with_garbage_tail(root: Path) -> Path:
    from erasure import HNSW_HEADER

    segment = root / "segment-uuid"
    segment.mkdir()
    (segment / "header.bin").write_bytes(HNSW_HEADER.pack(1, 0, 4, 1, 16, 8, 4))
    vectors = segment / "data_level0.bin"
    vectors.write_bytes(b"\0" * 16 + b"secret".ljust(48, b"x"))
    return vectors


def test_scrub_skips_and_reports_when_another_process_holds_the_lock(tmp_path, monkeypatch):
    import subprocess

    import erasure

    vectors = _segment_with_garbage_tail(tmp_path)
    src = str(Path(__file__).parent.parent / "src")
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, src, str(tmp_path)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setattr(erasure, "HNSW_LOCK_WAIT_SEC", 0.2)
        assert erasure.scrub_hnsw_tail(tmp_path) == (0, [str(vectors)])
        assert b"secret" in vectors.read_bytes()
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    assert erasure.scrub_hnsw_tail(tmp_path) == (1, [])
    assert b"secret" not in vectors.read_bytes()


def test_lock_is_reentrant_and_tolerates_a_missing_directory(tmp_path):
    from erasure import chroma_write_lock

    with chroma_write_lock(tmp_path / "absent") as outer, chroma_write_lock(tmp_path / "absent") as inner:
        assert outer and inner
    assert not (tmp_path / "absent").exists()


def test_save_still_upserts_when_the_file_lock_is_unavailable(live_server, monkeypatch):
    import erasure

    server, store, root = live_server
    if store.is_postgres:
        pytest.skip("personal memory is SQLite")
    monkeypatch.setattr(erasure, "_try_file_lock", lambda handle: False)
    monkeypatch.setattr(erasure, "HNSW_LOCK_WAIT_SEC", 0.05)
    if store._chroma_collection_for("text") is None:
        pytest.skip("ChromaDB is disabled")
    saved, _ = call(server, "memory_save", type="fact", project="p", content=OLD)
    vector = {"vector": [0.1] * 384, "model": "m", "provider": "p", "space": "text",
              "content_type": "text", "language": None}
    store._persist_record_embedding(saved["id"], vector, ktype="fact", project="p", sid="sess-erase",
                                    created_at="2026-09-28T00:00:00Z", content=OLD, use_chroma=True)
    collection = store._chroma_collection_for("text")
    assert str(saved["id"]) in collection.get(ids=[str(saved["id"])])["ids"]
