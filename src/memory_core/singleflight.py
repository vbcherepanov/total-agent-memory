"""Single-flight execution of identical concurrent calls.

When several threads ask for the same key at once, the first runs the
function and the others wait for its result instead of repeating the work.
A waiter that is not answered within `timeout` seconds, or whose leader
failed, runs the function itself, so a slow or broken leader never turns
into an error or an unbounded wait for anyone else.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from memory_core.telemetry import counters, op_timer

T = TypeVar("T")


@dataclass
class _Call(Generic[T]):
    done: threading.Event = field(default_factory=threading.Event)
    result: T | None = None
    failed: bool = False
    waiters: int = 0


class SingleFlight(Generic[T]):
    """Key -> in-flight call; thread-safe."""

    def __init__(self, name: str):
        self._name = name
        self._calls: dict[Hashable, _Call[T]] = {}
        self._lock = threading.Lock()

    def in_flight(self) -> int:
        """Keys being computed now."""
        with self._lock:
            return len(self._calls)

    def waiting(self) -> int:
        """Callers waiting for another thread's result now."""
        with self._lock:
            return sum(call.waiters for call in self._calls.values())

    def run(self, key: Hashable, fn: Callable[[], T], *, timeout: float) -> tuple[T, bool]:
        """Return `(result, shared)`; `shared` is True when another thread's execution answered."""
        with self._lock:
            call = self._calls.get(key)
            leader = call is None
            if leader:
                call = self._calls[key] = _Call()
            else:
                call.waiters += 1
        if leader:
            try:
                call.result = fn()
            except BaseException:
                call.failed = True
                raise
            finally:
                with self._lock:
                    self._calls.pop(key, None)
                call.done.set()
            return call.result, False
        with op_timer(f"{self._name}_singleflight_wait_ms"):
            answered = call.done.wait(timeout)
        with self._lock:
            call.waiters -= 1
        if not answered:
            counters.bump(f"{self._name}_singleflight_timeouts")
            return fn(), False
        if call.failed:
            counters.bump(f"{self._name}_singleflight_leader_failed")
            return fn(), False
        counters.bump(f"{self._name}_singleflight_shared")
        return call.result, True
