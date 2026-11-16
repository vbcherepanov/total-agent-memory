"""One cross-encoder re-rank per search, in the gateway, over the merged workspace candidates.

Workspace workers search with `defer_cross_rerank` and return their fused window. The gateway merges
the windows of the workspaces the caller may read, re-ranks the merged window once with the same
`CrossReranker` a single workspace used before, and keeps `limit` records. The model is loaded once,
lazily, in the gateway process; workers never load it. Encoder calls are serialised by a lock.
If the model is not ready (`MEMORY_CROSS_RERANK=auto`), fails, or exceeds the deadline, the merged
fused order is kept.
"""
import asyncio
import json
import logging
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)
CONTEXT_FIELD = "rerank_context"


@dataclass(frozen=True)
class Reranked:
    items: list[dict]
    status: str  # applied | skipped | not_ready | failed | timeout


def _default_encoder():
    from memory_core.cross_rerank import shared_reranker
    return shared_reranker()


class GatewayReranker:
    def __init__(self, encoder_factory: Callable[[], object | None] = _default_encoder,
                 wait_for_model: Callable[[], bool] | None = None):
        self._factory = encoder_factory
        self._wait_for_model = wait_for_model or self._mode_is_on
        self._lock = threading.Lock()
        self.counts = Counter()

    @staticmethod
    def _mode_is_on() -> bool:
        from config import get_cross_rerank_mode
        return get_cross_rerank_mode() == "on"

    def encoder(self):
        return self._factory()

    def window_for(self, query: str) -> int:
        """Candidates to collect for `query`; 0 when the cross-encoder is off or does not apply."""
        encoder = self.encoder()
        if encoder is None or not encoder.applies_to(query):
            return 0
        return encoder.window

    def _run(self, encoder, query: str, items: list[dict]) -> Reranked:
        with self._lock:
            ordered = encoder.rerank(
                query, items, lambda item: item["record"].get("content", ""),
                wait=self._wait_for_model(),
                context_of=(lambda window: [item["record"].get(CONTEXT_FIELD) or item["record"].get("content", "")
                                            for item in window]) if encoder.context_chars else None,
                recency_of=None)
        return Reranked(ordered, "applied" if encoder.ready else "not_ready")

    async def rerank(self, query: str, items: list[dict], deadline: float) -> Reranked:
        encoder = self.encoder()
        if encoder is None or len(items) < 2 or not encoder.applies_to(query):
            return self._count(Reranked(list(items), "skipped"))
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self._run, encoder, query, list(items)), deadline)
        except TimeoutError:
            LOGGER.warning(json.dumps({"event": "gateway_rerank_timeout", "deadline_seconds": deadline}))
            return self._count(Reranked(list(items), "timeout"))
        except Exception:  # onnxruntime raises its own types; the fused order stays valid
            LOGGER.exception("gateway_rerank_failed")
            return self._count(Reranked(list(items), "failed"))
        return self._count(result)

    def _count(self, result: Reranked) -> Reranked:
        self.counts[result.status] += 1
        return result
