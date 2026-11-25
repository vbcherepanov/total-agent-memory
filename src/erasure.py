"""Hard erasure of a personal-memory record: the row, its earlier versions and everything derived.

`memory_delete` without `hard` only hides a record. This removes it for good: SQLite
rows (triggers take the FTS index, atomic facts, graph links and evidence passages),
vectors in every embedding space, queue and log rows, quotes of it inside later
versions, agent action log rows that quote it, and the copies of its text in the raw
call log. `secure_delete` zeroes what this pass frees, a VACUUM rebuilds the database so
text freed by earlier ordinary writes goes too, and a WAL checkpoint truncates the log.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from ambient.actions import ActionLog

ERASED = "[erased]"
ACTION_LOG_TABLE = "agent_actions"
SECONDS_PER_DAY = 86_400
# Entry types the server writes to the raw call log (src/server.py `_call_tool_impl`).
RAW_CALL_TYPE = "tool_call"
RAW_RESULT_TYPE = "tool_result"

# Tables whose `knowledge_id` rows describe a record. Triggers on DELETE FROM knowledge
# already cover knowledge_fts, atomic_fact*, knowledge_nodes and passage_sources.
DERIVED_TABLES = (
    "embeddings", "enrichment_queue", "deep_enrichment_queue", "representations_queue",
    "triple_extraction_queue", "knowledge_representations", "knowledge_enrichment", "episode_facts",
    "entity_dedup_log", "filter_savings", "quality_gate_log", "write_intents", "knowledge_nodes_quarantine",
    "evidence_passages",
)
PAIR_TABLES = (("contradiction_log", ("new_knowledge_id", "candidate_knowledge_id")),
               ("relations", ("from_id", "to_id")))
# `memory_update` stores "Was: <first 200 chars of the old content>" in the new version's context.
QUOTED_PREFIX_CHARS = 200


class ErasureUnsupported(RuntimeError):
    """Hard erasure runs on the personal SQLite store only."""


def version_chain(db, record_id: int) -> list[int]:
    """`record_id` and every earlier version that was superseded into it, oldest last."""
    chain, frontier = [record_id], [record_id]
    while frontier:
        marks = ",".join("?" * len(frontier))
        rows = db.execute(f"SELECT id FROM knowledge WHERE superseded_by IN ({marks})", frontier).fetchall()
        frontier = [row[0] for row in rows if row[0] not in chain]
        chain.extend(frontier)
    return chain


def _existing_tables(db) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _fts_tables(db) -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%USING fts5%'")]


def _merge_fts(db) -> None:
    """FTS5 keeps deleted tokens in older index segments until a merge; `optimize` rewrites them."""
    for table in _fts_tables(db):
        db.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")


CHROMA_DELETE_OPERATION = 3


def compact_sqlite(db) -> str | None:
    """Rebuild the database file and truncate the WAL; the SQLite error text when that is refused.

    A row updated or deleted before the erasure leaves its old bytes in the free space of its
    page: `secure_delete` was off then, so only a rebuild removes them.
    """
    refused = None
    try:
        db.execute("VACUUM")
    except sqlite3.Error as error:
        refused = str(error)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return refused


def compact_chroma(chroma_dir: Path, ids: list[int]) -> None:
    """Remove the erased documents' text from Chroma's own SQLite file.

    Chroma replays writes from `embeddings_queue`, whose `metadata` holds the document text. The
    add/upsert entries of erased ids lose it (their DELETE entries stay for the vector index);
    then its FTS is merged and the file vacuumed.
    """
    path = chroma_dir / "chroma.sqlite3"
    if not path.is_file():
        return
    db = sqlite3.connect(path, timeout=30)
    try:
        db.execute("PRAGMA secure_delete=ON")
        with db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='embeddings_queue'").fetchone():
                marks = ",".join("?" * len(ids))
                db.execute(f"UPDATE embeddings_queue SET metadata=NULL WHERE id IN ({marks}) AND operation != ?",
                           (*[str(i) for i in ids], CHROMA_DELETE_OPERATION))
            _merge_fts(db)
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()


HNSW_VECTOR_FILE = "data_level0.bin"
HNSW_HEADER_FILE = "header.bin"
HNSW_HEADER = struct.Struct("<IQQQQQQ")  # version, offset_level0, max_elements, count, element_size, label_offset, data_offset
HNSW_HEADER_VERSION = 1


HNSW_LOCK_FILE = ".tam-hnsw.lock"
HNSW_LOCK_WAIT_SEC = 2.0
HNSW_LOCK_POLL_SEC = 0.05
SCRUB_CHUNK_BYTES = 1 << 20

_THREAD_LOCK = threading.RLock()
_HELD = threading.local()


def _try_file_lock(handle) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


@contextmanager
def chroma_write_lock(chroma_dir: Path, wait: float | None = None):
    """Serialise Chroma index writes and the HNSW scrub across threads and processes; yields whether it was taken.

    A thread lock plus an OS lock on a file inside `chroma_dir`, waited for at most `wait` seconds.
    Callers that get False carry on as they would without it. Re-entrant per thread. Processes of
    older versions do not take the lock. A missing `chroma_dir` is not created: only the thread lock applies.
    """
    wait = HNSW_LOCK_WAIT_SEC if wait is None else wait
    if getattr(_HELD, "depth", 0):
        _HELD.depth += 1
        try:
            yield True
        finally:
            _HELD.depth -= 1
        return
    if not _THREAD_LOCK.acquire(timeout=wait):
        yield False
        return
    handle = None
    acquired = False
    try:
        if chroma_dir.is_dir():
            try:
                handle = (chroma_dir / HNSW_LOCK_FILE).open("a+b")
                if not handle.seek(0, os.SEEK_END):
                    handle.write(b"\0")
                    handle.flush()
                deadline = time.monotonic() + wait
                acquired = _try_file_lock(handle)
                while not acquired and time.monotonic() < deadline:
                    time.sleep(HNSW_LOCK_POLL_SEC)
                    acquired = _try_file_lock(handle)
            except OSError:
                acquired = False
        else:
            acquired = True
        _HELD.depth = 1
        try:
            yield acquired
        finally:
            _HELD.depth = 0
    finally:
        if handle is not None:
            handle.close()
        _THREAD_LOCK.release()


def _zero_tail(handle, start: int, end: int) -> bool:
    """Zero `handle[start:end]` in chunks; False when it was all zero already."""
    changed = False
    handle.seek(start)
    position = start
    zeros = bytes(SCRUB_CHUNK_BYTES)
    while position < end:
        chunk = handle.read(min(SCRUB_CHUNK_BYTES, end - position))
        if not chunk:
            break
        if any(chunk):
            handle.seek(position)
            handle.write(zeros[:len(chunk)])
            changed = True
        position += len(chunk)
        handle.seek(position)
    if changed:
        handle.flush()
        os.fsync(handle.fileno())
    return changed


def scrub_hnsw_tail(chroma_dir: Path) -> tuple[int, list[str]]:
    """Zero the unused slots of every HNSW vector file; returns (files changed, files left as they were).

    The index allocates `max_elements` slots with plain `malloc` and writes the whole block when
    it is created, so slots past `cur_element_count` hold whatever the process heap held at that
    moment, including freed SQLite pages with record text. Later flushes write only the live
    prefix, and a resize rewrites the file at its live size, so one scrub is enough. Only the
    prefix described by the sibling `header.bin` is kept; files that do not match that layout are
    left alone. A file that cannot be opened for writing (locked on Windows), or every file when
    `chroma_write_lock` cannot be taken, is reported instead of raising.
    """
    scrubbed, failed = 0, []
    with chroma_write_lock(chroma_dir) as locked:
        for vectors in chroma_dir.glob(f"*/{HNSW_VECTOR_FILE}"):
            if not locked:
                failed.append(str(vectors))
                continue
            try:
                header = (vectors.parent / HNSW_HEADER_FILE).read_bytes()[:HNSW_HEADER.size]
                if len(header) < HNSW_HEADER.size:
                    continue
                version, _, capacity, count, element_size, _, _ = HNSW_HEADER.unpack(header)
                live = count * element_size
                size = vectors.stat().st_size
                if version != HNSW_HEADER_VERSION or element_size == 0 or count > capacity \
                        or size != capacity * element_size or live >= size:
                    continue
                with vectors.open("r+b") as handle:
                    scrubbed += _zero_tail(handle, live, size)
            except OSError:
                failed.append(str(vectors))
    return scrubbed, failed


def _linked_call_ids(paths: list[Path], record_ids: set[int]) -> set[str]:
    """Ids of logged tool calls whose `tool_result` line reports one of `record_ids`."""
    call_ids: set[str] = set()
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if RAW_RESULT_TYPE not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == RAW_RESULT_TYPE and record_ids.intersection(entry.get("knowledge_ids") or ()):
                call_ids.add(str(entry.get("call_id")))
    return call_ids


def _erase_call_args(line: str, call_ids: set[str]) -> str:
    """The logged call with its arguments blanked, when the call produced an erased record."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(entry, dict) or entry.get("type") != RAW_CALL_TYPE or str(entry.get("call_id")) not in call_ids:
        return line
    entry["args"] = ERASED
    newline = "\n" if line.endswith("\n") else ""
    return json.dumps(entry, ensure_ascii=False, default=str) + newline


def scrub_raw_logs(raw_dir: Path, texts: list[str], record_ids: list[int] | None = None) -> int:
    """Erase the record from the JSONL call logs; returns the number of files rewritten.

    The log holds what the caller *submitted*, which differs from the stored text whenever
    the save pipeline rewrote it (filters, coreference, redaction), so a text search alone
    misses it. Each logged call carries a `call_id`; the `tool_result` line written after it
    names the records it produced. Calls that produced an erased record lose their arguments;
    every remaining copy of `texts` is replaced as well.
    """
    needles = sorted({json.dumps(t, ensure_ascii=False)[1:-1] for t in texts if t and t.strip()}, key=len,
                     reverse=True)
    if not raw_dir.is_dir():
        return 0
    paths = sorted(raw_dir.glob("*.jsonl"))
    call_ids = _linked_call_ids(paths, set(record_ids)) if record_ids else set()
    if not needles and not call_ids:
        return 0
    rewritten = 0
    for path in paths:
        original = path.read_text(encoding="utf-8")
        cleaned = original
        if call_ids:
            cleaned = "".join(_erase_call_args(line, call_ids) for line in cleaned.splitlines(keepends=True))
        for needle in needles:
            cleaned = cleaned.replace(needle, ERASED)
        if cleaned == original:
            continue
        fd, temp = tempfile.mkstemp(dir=raw_dir, prefix=".erase-", suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(cleaned)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        rewritten += 1
    return rewritten


def erase(store, record_id: int, raw_dir: Path, chroma_dir: Path | None = None) -> dict | None:
    """Erase `record_id` with its earlier versions; None when no such record exists."""
    return erase_many(store, [record_id], raw_dir, chroma_dir)


def erase_many(store, record_ids: list[int], raw_dir: Path, chroma_dir: Path | None = None) -> dict | None:
    """Erase every record in `record_ids` with its earlier versions in one pass; None when none exists.

    One FTS merge, one WAL checkpoint and one raw-log scrub for the whole batch, so
    `tam forget-auto` costs the same for one record or a thousand.
    """
    if getattr(store, "is_postgres", False):
        raise ErasureUnsupported("Hard delete is available for personal (SQLite) memory only")
    db = store.db
    existing = [rid for rid in dict.fromkeys(record_ids)
                if db.execute("SELECT 1 FROM knowledge WHERE id=?", (rid,)).fetchone() is not None]
    if not existing:
        return None
    ids: list[int] = []
    for rid in existing:
        ids.extend(kid for kid in version_chain(db, rid) if kid not in ids)
    marks = ",".join("?" * len(ids))
    rows = db.execute(f"SELECT id, content, context, project FROM knowledge WHERE id IN ({marks})", ids).fetchall()
    texts = [value for row in rows for value in (row[1], row[2]) if value]
    projects = sorted({row[3] for row in rows if row[3]})
    spaces = {row[0] for row in db.execute(
        f"SELECT DISTINCT embedding_space FROM embeddings WHERE knowledge_id IN ({marks})", ids)}

    tables = _existing_tables(db)
    actions_erased = 0
    secure_delete_before = db.execute("PRAGMA secure_delete").fetchone()[0]
    db.execute("PRAGMA secure_delete=ON")
    try:
        for kid in ids:
            store._delete_embedding(kid)
        for table in DERIVED_TABLES:
            if table in tables:
                db.execute(f"DELETE FROM {table} WHERE knowledge_id IN ({marks})", ids)
        for table, columns in PAIR_TABLES:
            if table in tables:
                where = " OR ".join(f"{column} IN ({marks})" for column in columns)
                db.execute(f"DELETE FROM {table} WHERE {where}", ids * len(columns))
        for row in rows:
            quoted = (row[1] or "")[:QUOTED_PREFIX_CHARS]
            if quoted.strip():
                db.execute(f"UPDATE knowledge SET context=replace(context, ?, ?) "
                           f"WHERE instr(context, ?) > 0 AND id NOT IN ({marks})", (quoted, ERASED, quoted, *ids))
        if ACTION_LOG_TABLE in tables:
            # A logged command or edit can carry the record's text (src/ambient/actions.py).
            actions_erased = ActionLog(db).erase_mentions(texts)
        db.execute(f"DELETE FROM knowledge WHERE id IN ({marks})", ids)
        _merge_fts(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute(f"PRAGMA secure_delete={int(secure_delete_before)}")
    not_compacted = compact_sqlite(db)

    result = {"erased": ids, "projects": projects, "raw_logs_rewritten": scrub_raw_logs(raw_dir, texts, ids),
              "actions_erased": actions_erased}
    if not_compacted is not None:
        result["sqlite_not_compacted"] = not_compacted
    for space in sorted({"text", *spaces}):
        collection = store._chroma_collection_for(space)
        if collection is None:
            continue
        try:
            collection.delete(ids=[str(kid) for kid in ids])
        except Exception as error:  # noqa: BLE001 — chroma raises its own types; SQLite erasure already committed
            result.setdefault("vector_store_errors", []).append(f"{space}: {error}")
    if chroma_dir is not None and store._chroma_client is not None:
        try:
            compact_chroma(chroma_dir, ids)
        except sqlite3.Error as error:
            result.setdefault("vector_store_errors", []).append(f"compact: {error}")
        _, unscrubbed = scrub_hnsw_tail(chroma_dir)
        if unscrubbed:
            result["hnsw_files_not_scrubbed"] = unscrubbed
    return result


def prune_raw_logs(raw_dir: Path, days: str | None, now: float | None = None) -> int:
    """Remove call logs not written for `days` days; unset, blank or invalid keeps everything."""
    if not days or not str(days).strip().isdigit() or int(days) <= 0 or not raw_dir.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - int(days) * SECONDS_PER_DAY
    removed = 0
    for path in raw_dir.glob("*.jsonl"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed
