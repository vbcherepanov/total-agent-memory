from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from typing import Generic, TypeVar

from memory_core.store_owner import StoreOwner

T = TypeVar('T')
R = TypeVar('R')
REVISION_RETRIES = 2


@dataclass(frozen=True)
class SearchRequest:
    query: str
    project: str | None = None
    ktype: str = 'all'
    limit: int = 10
    detail: str = 'full'
    branch: str | None = None
    fusion: str = 'rrf'
    rerank: bool = False
    diverse: bool = False
    embedding_space: str | list[str] | None = None
    _explain: bool = False
    record_usage: bool = True
    defer_cross_rerank: bool = False
    tier_weights: dict[str, float] | None = None
    as_of: datetime | str | None = None

    def flight_key(self) -> str:
        options = asdict(self)
        del options['record_usage']
        # The query stays exact: the cross-encoder is case/whitespace sensitive, so "Foo" and "foo"
        # must not share a result. record_usage is excluded to keep the dedup benefit; every caller
        # that asked for usage records it itself after receiving the shared result (server.py).
        options['query'] = str(self.query or '')
        if isinstance(self.embedding_space, list):
            options['embedding_space'] = sorted(self.embedding_space)
        return json.dumps(options, sort_keys=True, default=str)


class SearchRevisionChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class InferenceCall(Generic[T]):
    execute: Callable[[], T]


def run_inline(steps: Generator[InferenceCall[T], T, R]) -> R:
    try:
        work = next(steps)
        while True:
            try:
                value = work.execute()
            except Exception as error:
                work = steps.throw(error)
            else:
                work = steps.send(value)
    except StopIteration as done:
        return done.value
    finally:
        steps.close()


async def run_staged(steps: Generator[InferenceCall[T], T, R], owner: StoreOwner) -> R:
    try:
        work = next(steps)
        while True:
            try:
                value = await owner.infer(work.execute)
            except Exception as error:
                work = steps.throw(error)
            else:
                work = steps.send(value)
    except StopIteration as done:
        return done.value
    finally:
        steps.close()
