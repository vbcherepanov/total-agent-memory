"""Search across multi-representation embeddings.

Given a query embedding, search `knowledge_representations` table (migration 002)
separately for each representation type (summary/keywords/questions — raw is
already covered by the main `embeddings` table) and fuse the per-representation
ranked lists via RRF.

Returns (knowledge_id, fused_score) pairs. If the table is empty or no matches
found, returns an empty list (safe no-op tier).

v9.0 Phase 1 (A1): when ``V9_PARALLEL_RETRIEVAL=1`` the per-representation
tiers are processed concurrently via ``asyncio.gather + asyncio.to_thread``
instead of the sequential v8 loop. Public API stays sync; callers (``server.py``)
need not be touched. Flag OFF → identical v8 code path.
"""

from __future__ import annotations

import asyncio
import sqlite3
import struct
import sys
import threading
from collections.abc import Iterable

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:
    from multi_repr_store import rrf_fuse
except ImportError:  # package path
    from .multi_repr_store import rrf_fuse  # type: ignore[no-redef]

try:
    import config as _config
except ImportError:  # package path
    from . import config as _config  # type: ignore[no-redef]

LOG = lambda msg: sys.stderr.write(f"[multi-repr-search] {msg}\n")


# LLM-generated views we search over (raw already covered by embeddings table)
_SEARCH_REPRESENTATIONS: tuple[str, ...] = (
    "summary", "keywords", "questions", "compressed",
)


def _cosine(a: list[float], b: list[float]) -> float:
    if np is None:
        num = sum(x * y for x, y in zip(a, b))
        da = sum(x * x for x in a) ** 0.5
        db = sum(y * y for y in b) ** 0.5
        if da == 0 or db == 0:
            return 0.0
        return num / (da * db)
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(va @ vb / (na * nb))


def _unpack_vector(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"{dim}f", blob))


def has_representations(db: sqlite3.Connection) -> bool:
    """Cheap existence check to gate this tier in hot path."""
    try:
        row = db.execute(
            "SELECT 1 FROM knowledge_representations LIMIT 1"
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _process_representation(
    db: sqlite3.Connection,
    db_lock: threading.Lock | None,
    repr_name: str,
    query_embedding: list[float],
    project: str | None,
    n_candidates: int,
    top_n: int,
) -> tuple[str, list[tuple[int, float]]]:
    """Fetch + score one representation. Returns (repr_name, scored_top_n).

    ``db_lock`` serializes SQLite access when the connection happens to be
    shared across threads (``check_same_thread=False``). When the default
    connection is used (single-thread-only), the parallel path delegates
    fetching to the main thread and only the cosine compute runs under
    ``to_thread`` — see ``_score_rows``.
    """
    try:
        if db_lock is not None:
            with db_lock:
                rows = _fetch(db, repr_name, project, n_candidates, len(query_embedding))
        else:
            rows = _fetch(db, repr_name, project, n_candidates, len(query_embedding))
    except sqlite3.Error as e:
        LOG(f"fetch error for representation={repr_name}: {e}")
        return repr_name, []

    return repr_name, _score_rows(rows, query_embedding, top_n)


def _score_rows(
    rows: list[sqlite3.Row] | Iterable[sqlite3.Row],
    query_embedding: list[float],
    top_n: int,
) -> list[tuple[int, float]]:
    """Pure-python cosine scoring. Safe to call from any thread (no sqlite)."""
    if not rows:
        return []
    dim = len(query_embedding)
    if np is not None:
        return _score_rows_numpy(rows, query_embedding, top_n, dim)
    scored: list[tuple[int, float]] = []
    for r in rows:
        try:
            vec = _unpack_vector(r["float32_vector"], r["embed_dim"])
        except (struct.error, KeyError):
            continue
        if len(vec) != dim:
            # Dim mismatch — different embedder. Skip silently.
            continue
        sim = _cosine(query_embedding, vec)
        if sim > 0:
            scored.append((r["knowledge_id"], sim))
    if not scored:
        return []
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored[:top_n]


def _score_rows_numpy(rows, query_embedding, top_n: int, dim: int) -> list[tuple[int, float]]:
    """One matrix product over every row of the scope instead of a Python loop per row."""
    ids: list[int] = []
    blobs: list[bytes] = []
    expected = dim * 4
    for r in rows:
        blob = r["float32_vector"]
        if int(r["embed_dim"] or 0) != dim or blob is None or len(blob) != expected:
            continue
        ids.append(int(r["knowledge_id"]))
        blobs.append(bytes(blob))
    if not ids:
        return []
    matrix = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(ids), dim)
    query = np.asarray(query_embedding, dtype=np.float32)
    query_norm = float(np.linalg.norm(query))
    if query_norm == 0:
        return []
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = np.inf
    sims = (matrix @ query) / (norms * query_norm)
    order = np.argsort(-sims, kind="stable")[:top_n]
    return [(ids[int(i)], float(sims[int(i)])) for i in order if float(sims[int(i)]) > 0]


def _search_sequential(
    db: sqlite3.Connection,
    query_embedding: list[float],
    project: str | None,
    n_candidates: int,
    top_n: int,
) -> dict[str, list[tuple[int, float]]]:
    """v8 path — sequential per-representation processing."""
    per_repr: dict[str, list[tuple[int, float]]] = {}
    for repr_name in _SEARCH_REPRESENTATIONS:
        name, scored = _process_representation(
            db, None, repr_name, query_embedding, project, n_candidates, top_n
        )
        if scored:
            per_repr[name] = scored
    return per_repr


async def _search_parallel_async(
    db: sqlite3.Connection,
    query_embedding: list[float],
    project: str | None,
    n_candidates: int,
    top_n: int,
) -> dict[str, list[tuple[int, float]]]:
    """v9.0 A1 path — concurrent per-representation via asyncio.gather.

    Strategy:
      1. Run all 4 ``_fetch`` calls on the current thread (sqlite3's default
         ``check_same_thread=True`` forbids cross-thread connection use).
         These are LIMIT-capped SELECTs and complete in milliseconds.
      2. Run the per-representation cosine scoring via ``asyncio.gather +
         asyncio.to_thread``. numpy's matrix ops release the GIL, so workers
         actually run in parallel on multi-core boxes.

    Tier failures (either phase) are absorbed: WARN + empty ranked list for
    that representation. One bad tier must not crash the tier ensemble.
    """
    # Phase 1: fetch (serial, on caller's thread — safe for any sqlite3 conn).
    fetched: dict[str, list[sqlite3.Row]] = {}
    for repr_name in _SEARCH_REPRESENTATIONS:
        try:
            rows = _fetch(db, repr_name, project, n_candidates, len(query_embedding))
        except sqlite3.Error as e:
            LOG(f"fetch error for representation={repr_name}: {e}")
            continue
        if rows:
            fetched[repr_name] = rows

    if not fetched:
        return {}

    # Phase 2: score in parallel via asyncio.gather + to_thread.
    async def _score_one(repr_name: str, rows: list[sqlite3.Row]):
        scored = await asyncio.to_thread(
            _score_rows, rows, query_embedding, top_n
        )
        return repr_name, scored

    coros = [_score_one(name, rows) for name, rows in fetched.items()]
    results = await asyncio.gather(*coros, return_exceptions=True)

    per_repr: dict[str, list[tuple[int, float]]] = {}
    for idx, res in enumerate(results):
        if isinstance(res, BaseException):
            repr_name = list(fetched.keys())[idx]
            LOG(f"tier failed representation={repr_name}: {res!r}")
            continue
        name, scored = res
        if scored:
            per_repr[name] = scored
    return per_repr


def _run_coroutine(coro):
    """Run ``coro`` to completion whether or not a loop is already running.

    If called from a sync context with no running loop, uses ``asyncio.run``.
    If the current thread already has a running loop (rare here — server.py
    is sync), executes the coroutine on a fresh loop in a worker thread to
    avoid ``RuntimeError: asyncio.run() cannot be called from a running loop``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — simple path.
        return asyncio.run(coro)

    # Running loop present: bounce into a helper thread that owns its own loop.
    import concurrent.futures

    def _runner():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_runner).result()


def search(
    db: sqlite3.Connection,
    query_embedding: list[float],
    project: str | None = None,
    n_candidates: int = 0,
    top_n: int = 20,
) -> list[tuple[int, float]]:
    """Search each representation, fuse with RRF, return (knowledge_id, score).

    Scores returned are RRF fusion scores (not cosine similarities) — use as
    one tier among many in the caller's fusion.

    When ``V9_PARALLEL_RETRIEVAL=1`` the per-representation tiers are
    processed concurrently; otherwise falls back to the v8 sequential path.
    The public return contract is identical in both modes.
    """
    fused, _ = search_with_winners(
        db, query_embedding, project, n_candidates, top_n
    )
    return fused


def search_with_winners(
    db: sqlite3.Connection,
    query_embedding: list[float],
    project: str | None = None,
    n_candidates: int = 0,
    top_n: int = 20,
) -> tuple[list[tuple[int, float]], dict[int, str]]:
    """Same as ``search`` but also returns ``winners``.

    ``winners[knowledge_id] = best_repr_name`` — the representation type
    (summary / keywords / questions / compressed) that produced the highest
    cosine similarity for that record. Callers can use this to apply a
    per-representation staleness decay: a hit via ``summary`` ages faster
    than a hit via ``raw`` because LLM-generated views encode dated context.

    Empty winners dict when no representations matched.
    """
    if not query_embedding:
        return [], {}

    if _config.is_v9_parallel_retrieval_enabled():
        per_repr = _run_coroutine(
            _search_parallel_async(
                db, query_embedding, project, n_candidates, top_n
            )
        )
    else:
        per_repr = _search_sequential(
            db, query_embedding, project, n_candidates, top_n
        )

    if not per_repr:
        return [], {}

    fused = rrf_fuse(per_repr, k=60, top_n=top_n)

    # Resolve winner per knowledge_id: the repr that scored it highest (cosine).
    winners: dict[int, tuple[str, float]] = {}
    for repr_name, scored in per_repr.items():
        for kid, sim in scored:
            current = winners.get(kid)
            if current is None or sim > current[1]:
                winners[kid] = (repr_name, sim)

    return fused, {kid: name for kid, (name, _) in winners.items()}


def _fetch(
    db: sqlite3.Connection,
    representation: str,
    project: str | None,
    limit: int,
    dim: int | None = None,
) -> list[sqlite3.Row]:
    """Representation vectors of the scope.

    Every row of the scope is scored: until 14.8.0 the query took the first
    ``limit`` rows in table order, so on a store with more representations
    than that the tier compared the query with an arbitrary old subset and
    never saw newer records. ``limit`` is now only a safety cap (0 = none)
    and rows of another embedding dimension are left out in SQL.
    """
    conds = ["kr.representation = ?", "k.status = 'active'"]
    params: list[object] = [representation]
    if project:
        conds.append("k.project = ?")
        params.append(project)
    if dim:
        conds.append("kr.embed_dim = ?")
        params.append(int(dim))
    tail = ""
    if limit and limit > 0:
        tail = " LIMIT ?"
        params.append(int(limit))
    return db.execute(
        f"""SELECT kr.knowledge_id, kr.float32_vector, kr.embed_dim
             FROM knowledge_representations kr
             JOIN knowledge k ON k.id = kr.knowledge_id
            WHERE {' AND '.join(conds)}{tail}""",
        params,
    ).fetchall()
