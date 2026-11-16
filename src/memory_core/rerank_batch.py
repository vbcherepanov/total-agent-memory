from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from memory_core.telemetry import counters, op_timer


class BatchedEncoder(Protocol):
    def rerank(self, query: str, documents: Iterable[str], *, batch_size: int) -> Iterable[float]: ...


class LengthBatchedEncoder:
    def __init__(self, encoder: BatchedEncoder, batch_size: int):
        if batch_size < 1:
            raise ValueError('Cross-encoder batch size must be positive')
        self._encoder = encoder
        self._batch_size = batch_size

    def rerank(self, query: str, documents: Iterable[str]) -> list[float]:
        texts = list(documents)
        if not texts:
            return []
        order = sorted(range(len(texts)), key=lambda index: len(texts[index]))
        counters.bump('cross_rerank_batched_documents', len(texts))
        with op_timer('cross_rerank_batched_ms'):
            scores = list(self._encoder.rerank(query, [texts[index] for index in order],
                                              batch_size=self._batch_size))
        if len(scores) != len(texts):
            raise RuntimeError(f'cross-encoder returned {len(scores)} scores for {len(texts)} candidates')
        restored = [0.0] * len(texts)
        for index, score in zip(order, scores):
            restored[index] = float(score)
        return restored
