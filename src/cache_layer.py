"""
Two-level cache layer for Claude Memory v9.0 (lane A2).

This module adds a query/embedding cache stack on top of the existing
``QueryCache`` (``src/cache.py``). It is gated by ``V9_CACHE_L1_ENABLED`` /
``V9_CACHE_L2_ENABLED`` feature flags so that importing it is always safe
even when the caller has not opted in.

Design
------
L1 — in-process LRU with TTL:
    Key  : sha256(query | mode | k | filters_blob)
    Value: recall result payload (arbitrary JSON-serialisable object)
    Size : ``V9_CACHE_L1_SIZE``     (default 1000 entries)
    TTL  : ``V9_CACHE_L1_TTL_SEC``  (default 300 seconds)
    Goal : <1ms hit latency, ~35-50% hit ratio on warm traffic.

L2 — SQLite persistent embedding cache (table ``embedding_cache``):
    Key   : sha256(text)
    Value : packed float32 BLOB + model + dim
    Goal  : 2-3ms hit latency. Survives process restart.

Invalidation
------------
L1 is invalidated on ``memory_save`` / update / delete — full wipe is
cheap and avoids stale hits across projects. L2 is never invalidated
by writes because sha256(text) is stable; stale entries only age out
through optional background cleanup.

All classes are thread-safe:
    • L1 uses a ``threading.Lock`` around the LRU ``OrderedDict``.
    • L2 uses ``sqlite3.connect(..., check_same_thread=False)`` with
      a dedicated write lock. Read-paths reuse the same connection;
      SQLite serialises writes via WAL.

No-op mode
----------
When the corresponding feature flag is OFF the public API is still
available but ``get()`` always returns ``None`` and ``set()`` becomes
a no-op. This lets callers unconditionally wrap their hot paths
without branching on flag state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from paths import memory_dir

try:
    # Prefer the central config helpers so env semantics stay consistent.
    from config import (
        get_v9_cache_l1_size,
        get_v9_cache_l1_ttl_sec,
        is_v9_cache_l1_enabled,
        is_v9_cache_l2_enabled,
    )
except Exception:  # pragma: no cover - config import shouldn't fail in prod
    def is_v9_cache_l1_enabled() -> bool:  # type: ignore[misc]
        return os.environ.get("V9_CACHE_L1_ENABLED", "").strip().lower() in (
            "1", "true", "yes", "on"
        )

    def is_v9_cache_l2_enabled() -> bool:  # type: ignore[misc]
        return os.environ.get("V9_CACHE_L2_ENABLED", "").strip().lower() in (
            "1", "true", "yes", "on"
        )

    def get_v9_cache_l1_size() -> int:  # type: ignore[misc]
        try:
            return int(os.environ.get("V9_CACHE_L1_SIZE", "1000"))
        except ValueError:
            return 1000

    def get_v9_cache_l1_ttl_sec() -> float:  # type: ignore[misc]
        try:
            return float(os.environ.get("V9_CACHE_L1_TTL_SEC", "300"))
        except ValueError:
            return 300.0


_LOG = logging.getLogger("claude_memory.cache_layer")


# ──────────────────────────────────────────────────────────────
# Key helpers
# ──────────────────────────────────────────────────────────────


def make_l1_key(
    query: str,
    mode: str | None = None,
    k: int | None = None,
    filters: dict[str, Any] | None = None,
) -> str:
    """Build a deterministic L1 cache key.

    The key includes the query text, recall mode, top-k and a stable
    JSON serialisation of the filter dict. JSON is sorted by key to
    avoid insertion-order mismatches.
    """
    q = (query or "").strip()
    m = (mode or "").strip()
    k_str = "" if k is None else str(int(k))
    f_blob = json.dumps(filters or {}, sort_keys=True, default=str, ensure_ascii=False)
    raw = f"{q}\x1f{m}\x1f{k_str}\x1f{f_blob}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_l2_key(text: str) -> str:
    """Build a deterministic L2 key from the raw embedding input text."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────────────────────
# L1 — in-memory LRU + TTL
# ──────────────────────────────────────────────────────────────


@dataclass
class _L1Entry:
    value: Any
    expires_at: float
    memory_ids: tuple[int, ...]  # ids this result references, for targeted invalidation


class L1QueryCache:
    """Thread-safe LRU cache with TTL for query→results.

    This is a superset of ``cache.QueryCache`` tailored to the A2 lane
    needs: it accepts a list of ``memory_ids`` alongside each entry so
    that invalidation can target records individually (``invalidate_by_id``).

    When the feature flag is OFF the cache behaves as a transparent
    no-op: ``get`` returns ``None``, ``set`` does nothing.
    """

    def __init__(
        self,
        maxsize: int | None = None,
        ttl_sec: float | None = None,
        enabled: bool | None = None,
    ) -> None:
        # Resolve from env/flags when caller doesn't override.
        self._enabled_override = enabled
        self._maxsize = maxsize if maxsize is not None else max(1, get_v9_cache_l1_size())
        self._ttl = ttl_sec if ttl_sec is not None else max(0.0, get_v9_cache_l1_ttl_sec())
        self._cache: OrderedDict[str, _L1Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ── flag resolution ────────────────────────────────────

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        return is_v9_cache_l1_enabled()

    # ── public API ─────────────────────────────────────────

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            if time.time() > entry.expires_at:
                # Expired — drop and miss.
                del self._cache[key]
                self._misses += 1
                return None
            self._cache.move_to_end(key)
            self._hits += 1
            return entry.value

    def set(
        self,
        key: str,
        value: Any,
        memory_ids: Iterable[int] | None = None,
        ttl_sec: float | None = None,
    ) -> None:
        if not self.enabled:
            return
        ttl = self._ttl if ttl_sec is None else max(0.0, float(ttl_sec))
        ids = tuple(int(i) for i in (memory_ids or ()))
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = _L1Entry(
                value=value,
                expires_at=time.time() + ttl,
                memory_ids=ids,
            )
            # Evict LRU tail until under maxsize.
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def invalidate_all(self) -> int:
        """Drop every entry. Returns the number of removed entries."""
        with self._lock:
            n = len(self._cache)
            self._cache.clear()
            return n

    def invalidate_by_id(self, memory_id: int) -> int:
        """Drop entries whose payload referenced ``memory_id``.

        Safer than ``invalidate_all`` when many concurrent projects share
        the server. Returns the number of evictions.
        """
        target = int(memory_id)
        with self._lock:
            keys = [k for k, e in self._cache.items() if target in e.memory_ids]
            for k in keys:
                del self._cache[k]
            return len(keys)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            ratio = (self._hits / total) if total else 0.0
            return {
                "enabled": self.enabled,
                "hits": self._hits,
                "misses": self._misses,
                "hit_ratio": round(ratio, 4),
                "size": len(self._cache),
                "maxsize": self._maxsize,
                "ttl_sec": self._ttl,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


# ──────────────────────────────────────────────────────────────
# L2 — SQLite embedding cache
# ──────────────────────────────────────────────────────────────


def _pack_embedding(vec: Iterable[float]) -> bytes:
    vec_list = [float(x) for x in vec]
    return struct.pack(f"{len(vec_list)}f", *vec_list)


def _unpack_embedding(blob: bytes, dim: int) -> list[float]:
    if dim <= 0 or len(blob) != dim * 4:
        raise ValueError(f"embedding blob size mismatch: {len(blob)} bytes for dim={dim}")
    return list(struct.unpack(f"{dim}f", blob))


class L2EmbeddingCache:
    """SQLite-backed cache for embedding vectors keyed by ``sha256(text)``.

    The cache uses the same DB path as the main memory store but manages
    a dedicated connection opened with ``check_same_thread=False`` plus a
    write lock so that concurrent recall workers can share it safely.

    Hot-path design:
        • ``get(text, expected_dim, expected_model)`` — 1 SELECT, returns
          ``None`` on miss, dim mismatch, or model mismatch.
        • ``set(text, vector, model)`` — 1 INSERT OR REPLACE, best-effort
          (failures are logged and silently tolerated so that an overloaded
          writer never breaks the recall path).
    """

    _DEFAULT_DB_PATH = memory_dir() / "memory.db"

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        enabled: bool | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._enabled_override = enabled
        self._write_lock = threading.Lock()
        self._owns_conn = connection is None
        self._closed = False

        if connection is not None:
            self._conn = connection
        else:
            path = Path(db_path) if db_path else self._DEFAULT_DB_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(path),
                check_same_thread=False,
                isolation_level=None,  # autocommit — we manage transactions explicitly
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")

        self._ensure_table()

    # ── flag resolution ────────────────────────────────────

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        return is_v9_cache_l2_enabled()

    # ── schema bootstrap ───────────────────────────────────

    def _ensure_table(self) -> None:
        """Create the table if the migration runner has not yet.

        The main server applies ``migrations/014_embedding_cache.sql`` on
        startup, but tests (and any standalone instantiation) may build
        this cache against a DB that doesn't have the table yet.
        """
        try:
            with self._write_lock:
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS embedding_cache (
                        key        TEXT PRIMARY KEY,
                        embedding  BLOB NOT NULL,
                        created_at TEXT NOT NULL,
                        model      TEXT NOT NULL,
                        dim        INTEGER NOT NULL
                    )
                    """
                )
        except sqlite3.Error as e:  # pragma: no cover - boot failure is fatal
            _LOG.warning("L2 embedding_cache bootstrap failed: %s", e)

    # ── public API ─────────────────────────────────────────

    def get(
        self,
        text: str,
        expected_dim: int | None = None,
        expected_model: str | None = None,
    ) -> list[float] | None:
        """Return cached vector or ``None`` on any kind of miss.

        Dim/model mismatches are treated as misses to protect the caller
        from feeding the wrong shape into a downstream index.
        """
        if not self.enabled or self._closed:
            return None
        key = make_l2_key(text)
        try:
            row = self._conn.execute(
                "SELECT embedding, dim, model FROM embedding_cache WHERE key = ?",
                (key,),
            ).fetchone()
        except sqlite3.Error as e:
            _LOG.warning("L2 get failed (key=%s): %s", key[:12], e)
            return None
        if row is None:
            return None
        blob, dim, model = row[0], int(row[1]), row[2]
        if expected_dim is not None and dim != int(expected_dim):
            return None  # safety: wrong shape
        if expected_model is not None and expected_model and model != expected_model:
            return None  # safety: wrong provider
        try:
            return _unpack_embedding(blob, dim)
        except ValueError as e:
            _LOG.warning("L2 unpack failed (key=%s): %s", key[:12], e)
            return None

    def set(
        self,
        text: str,
        vector: Iterable[float],
        model: str,
    ) -> bool:
        """Persist an embedding. Returns True on success, False on swallowed error."""
        if not self.enabled or self._closed:
            return False
        vec = [float(x) for x in vector]
        if not vec:
            return False
        key = make_l2_key(text)
        blob = _pack_embedding(vec)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            with self._write_lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO embedding_cache "
                    "(key, embedding, created_at, model, dim) VALUES (?, ?, ?, ?, ?)",
                    (key, blob, now, model or "", len(vec)),
                )
            return True
        except sqlite3.Error as e:
            # Graceful: recall must survive transient DB contention.
            _LOG.warning("L2 set failed (key=%s): %s", key[:12], e)
            return False

    def size(self) -> int:
        if self._closed:
            return 0
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM embedding_cache"
            ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def purge_all(self) -> int:
        """Drop every cached embedding. Intended for tests and maintenance."""
        if self._closed:
            return 0
        try:
            with self._write_lock:
                cur = self._conn.execute("DELETE FROM embedding_cache")
            return cur.rowcount or 0
        except sqlite3.Error as e:
            _LOG.warning("L2 purge failed: %s", e)
            return 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_conn:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


# ──────────────────────────────────────────────────────────────
# Facade
# ──────────────────────────────────────────────────────────────


class TwoLevelCache:
    """Convenience facade binding ``L1QueryCache`` + ``L2EmbeddingCache``.

    The server holds one instance at ``Store.v9_cache``. Individual
    layers stay usable through ``.l1`` / ``.l2`` attributes for code
    paths that only touch one tier.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        l1: L1QueryCache | None = None,
        l2: L2EmbeddingCache | None = None,
    ) -> None:
        self.l1 = l1 if l1 is not None else L1QueryCache()
        self.l2 = l2 if l2 is not None else L2EmbeddingCache(db_path=db_path)

    # ── L1 helpers ─────────────────────────────────────────

    def recall_get(
        self,
        query: str,
        mode: str | None = None,
        k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> Any | None:
        return self.l1.get(make_l1_key(query, mode, k, filters))

    def recall_set(
        self,
        query: str,
        value: Any,
        mode: str | None = None,
        k: int | None = None,
        filters: dict[str, Any] | None = None,
        memory_ids: Iterable[int] | None = None,
    ) -> None:
        self.l1.set(
            make_l1_key(query, mode, k, filters),
            value,
            memory_ids=memory_ids,
        )

    def invalidate_all(self) -> int:
        return self.l1.invalidate_all()

    def invalidate_by_id(self, memory_id: int) -> int:
        return self.l1.invalidate_by_id(memory_id)

    # ── L2 helpers ─────────────────────────────────────────

    def embed_get(
        self,
        text: str,
        expected_dim: int | None = None,
        expected_model: str | None = None,
    ) -> list[float] | None:
        return self.l2.get(text, expected_dim=expected_dim, expected_model=expected_model)

    def embed_set(self, text: str, vector: Iterable[float], model: str) -> bool:
        return self.l2.set(text, vector, model)

    # ── misc ──────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {"l1": self.l1.stats(), "l2_size": self.l2.size()}

    def close(self) -> None:
        self.l2.close()


__all__ = [
    "L1QueryCache",
    "L2EmbeddingCache",
    "TwoLevelCache",
    "make_l1_key",
    "make_l2_key",
]
