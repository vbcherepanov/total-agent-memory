"""Re-export of `src/reflection/agent.py`.

The reflection agent is the "sleep cycle" of the memory: digest +
synthesize phases that consolidate, dedup, and evolve the knowledge
graph. Every phase in the agent is allowed to call the LLM, which is
why it lives under `ai_layer`.

`run_full` is the canonical entry point used by the scheduler; `run`
dispatches to `run_quick`/`run_full`/`run_weekly` based on scope.
"""

from __future__ import annotations

from collections.abc import Callable

from memory_core.embedding_identity import EmbedIdentity
from reflection.agent import ReflectionAgent  # noqa: F401  (re-export)


async def run_full(
    db,
    *,
    embedder=None,
    embed_identity: Callable[[], EmbedIdentity | None] | None = None,
):
    """Convenience wrapper: build a ReflectionAgent and call `run_full`.

    Mirrors `ReflectionAgent(db, embedder).run_full()` so callers that
    only need a one-shot full pass can do a single import.
    """
    return await ReflectionAgent(db, embedder=embedder, embed_identity=embed_identity).run_full()


__all__ = ["ReflectionAgent", "run_full"]
