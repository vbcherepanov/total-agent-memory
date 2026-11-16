from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable

from memory_core.telemetry import counters, op_timer

KEY_LENGTH_BYTES = 8


class ScoreCacheMiss(LookupError):
    pass


class RerankScoreCache:
    def __init__(self, max_scores: int):
        if max_scores < 0:
            raise ValueError('Reranker cache capacity must be nonnegative')
        self.max_scores = max_scores
        self._entries: OrderedDict[bytes, tuple[float, ...]] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    @staticmethod
    def _key(query: str, texts: list[str]) -> bytes:
        digest = hashlib.sha256()
        for text in (query, *texts):
            encoded = text.encode('utf-8')
            digest.update(len(encoded).to_bytes(KEY_LENGTH_BYTES, 'big'))
            digest.update(encoded)
        return digest.digest()

    def score(self, query: str, texts: list[str],
              infer: Callable[[str, list[str]], Iterable[float]], *, cache_only: bool = False) -> list[float]:
        if not texts:
            return []
        cacheable = 0 < len(texts) <= self.max_scores
        order = sorted(range(len(texts)), key=texts.__getitem__) if cacheable else []
        key = self._key(query, [texts[index] for index in order]) if cacheable else None
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                counters.bump('cross_rerank_score_cache_hits')
                restored = [0.0] * len(texts)
                for index, value in zip(order, cached):
                    restored[index] = value
                return restored
        if cache_only:
            raise ScoreCacheMiss('Cross-encoder scores are not cached')
        counters.bump('cross_rerank_score_cache_misses')
        with op_timer('cross_rerank_inference_ms'):
            scores = tuple(float(value) for value in infer(query, texts))
        if len(scores) != len(texts):
            raise RuntimeError(f'cross-encoder returned {len(scores)} scores for {len(texts)} candidates')
        if key is not None:
            with self._lock:
                previous = self._entries.pop(key, ())
                self._size -= len(previous)
                self._entries[key] = tuple(scores[index] for index in order)
                self._size += len(scores)
                while self._size > self.max_scores:
                    _, removed = self._entries.popitem(last=False)
                    self._size -= len(removed)
                    counters.bump('cross_rerank_score_cache_evictions')
        return list(scores)
