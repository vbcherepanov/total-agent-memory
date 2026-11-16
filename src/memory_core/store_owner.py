"""Bounded, serial execution on the thread that owns a personal Store."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Generic, TypeVar

from memory_core.telemetry import LATENCY_BUCKETS_MS, counters, op_timer

T = TypeVar("T")
DEFAULT_PENDING = 32
DEFAULT_INFERENCE_WORKERS = 4
DEFAULT_SHUTDOWN_SECONDS = 10.0
LOG = logging.getLogger(__name__)


def _observe_wait(name: str, elapsed_ms: float) -> None:
    counters.bump(name, elapsed_ms)
    counters.bump(f"{name}_count")
    for bound in LATENCY_BUCKETS_MS:
        if elapsed_ms <= bound:
            counters.bump(f"{name}_bucket_le_{bound}")
    counters.bump(f"{name}_bucket_le_inf")


class OwnerUnavailable(RuntimeError):
    pass


class OwnerOverloaded(OwnerUnavailable):
    pass


@dataclass(frozen=True)
class OwnerSettings:
    max_pending: int = DEFAULT_PENDING
    shutdown_seconds: float = DEFAULT_SHUTDOWN_SECONDS
    inference_workers: int = DEFAULT_INFERENCE_WORKERS

    def __post_init__(self) -> None:
        if isinstance(self.inference_workers, bool) or not isinstance(self.inference_workers, int) or self.inference_workers < 1:
            raise ValueError("TAM_RECALL_INFERENCE_WORKERS must be a positive integer")
        if self.max_pending < 1:
            raise ValueError("TAM_STORE_MAX_PENDING must be positive")
        if not math.isfinite(self.shutdown_seconds) or self.shutdown_seconds <= 0:
            raise ValueError("TAM_STORE_SHUTDOWN_SECONDS must be finite and positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> OwnerSettings:
        return cls(int(env.get("TAM_STORE_MAX_PENDING", DEFAULT_PENDING)),
                   float(env.get("TAM_STORE_SHUTDOWN_SECONDS", DEFAULT_SHUTDOWN_SECONDS)),
                   int(env.get("TAM_RECALL_INFERENCE_WORKERS", DEFAULT_INFERENCE_WORKERS)))


@dataclass
class _Job(Generic[T]):
    operation: Callable[[], Coroutine[None, None, T]]
    result: Future[T]
    context: contextvars.Context
    queued_at: float


class StoreOwner:
    def __init__(self, bootstrap: Callable[[], Coroutine[None, None, None]],
                 cleanup: Callable[[], Coroutine[None, None, None]], settings: OwnerSettings):
        self.settings = settings
        self._bootstrap, self._cleanup = bootstrap, cleanup
        self._condition = threading.Condition()
        self._jobs: deque[_Job] = deque()
        self._closing = False
        self._active = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._lease: asyncio.Lock | None = None
        self._lease_task: asyncio.Task | None = None
        self._lease_released: set[asyncio.Task] = set()
        self._pool: ThreadPoolExecutor | None = None
        self._ready: Future[None] = Future()
        self._done: Future[None] = Future()
        self._thread = threading.Thread(target=self._run, name="tam-store-owner", daemon=True)
        from memory_core.exit_join import join_at_exit
        join_at_exit(self._thread, settings.shutdown_seconds, stop=self.stop)
        self._thread.start()

    @property
    def is_owner(self) -> bool:
        return threading.current_thread() is self._thread

    async def ready(self) -> None:
        await asyncio.shield(asyncio.wrap_future(self._ready))

    def submit(self, operation: Callable[[], Coroutine[None, None, T]]) -> Future[T]:
        with self._condition:
            if self._closing or not self._ready.done() or self._ready.exception() is not None:
                raise OwnerUnavailable("Personal Store is not accepting calls")
            if len(self._jobs) + self._active >= self.settings.max_pending + 1:
                counters.bump("store_owner_rejected")
                raise OwnerOverloaded("Personal Store queue is full; retry later")
            result: Future[T] = Future()
            self._jobs.append(_Job(operation, result, contextvars.copy_context(), time.monotonic()))
            counters.bump("store_owner_pending", 1)
            counters.bump("store_owner_submitted")
            result.add_done_callback(self._discard_cancelled)
            self._notify()
            return result

    def _discard_cancelled(self, result: Future) -> None:
        if not result.cancelled():
            return
        with self._condition:
            for job in self._jobs:
                if job.result is result:
                    self._jobs.remove(job)
                    counters.bump("store_owner_pending", -1)
                    counters.bump("store_owner_cancelled_before_start")
                    break

    async def call(self, operation: Callable[[], Coroutine[None, None, T]]) -> T:
        if self.is_owner:
            raise RuntimeError("Recursive Store owner submission would deadlock")
        result = self.submit(operation)
        try:
            return await asyncio.wrap_future(result)
        except asyncio.CancelledError:
            result.cancel()  # Only queued work can be cancelled; a running write finishes once.
            counters.bump("store_owner_client_cancelled")
            raise

    def stop(self) -> None:
        with self._condition:
            self._closing = True
            while self._jobs:
                job = self._jobs.popleft()
                counters.bump("store_owner_pending", -1)
                if job.result.set_running_or_notify_cancel():
                    job.result.set_exception(OwnerUnavailable("Personal Store is shutting down"))
            self._notify()

    async def close(self) -> None:
        self.stop()
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self._done)),
                                   self.settings.shutdown_seconds)
        except TimeoutError:
            counters.bump("store_owner_shutdown_timeouts")
            LOG.error("Store owner still running after shutdown deadline",
                      extra={"timeout_seconds": self.settings.shutdown_seconds})
            raise

    def _notify(self) -> None:
        if self._loop is not None and self._wake is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._wake.set)

    async def suspend(self, waiting: Awaitable[T]) -> T:
        task = asyncio.current_task()
        if not self.is_owner or task is not self._lease_task or self._lease is None:
            raise RuntimeError("Only the active Store owner operation may suspend")
        self._lease_task = None
        self._lease.release()
        # The lease is not held until re-acquired; a cancellation at that await must not
        # make _execute_job release a lock it does not own.
        self._lease_released.add(task)
        try:
            return await waiting
        finally:
            await self._lease.acquire()
            self._lease_released.discard(task)
            self._lease_task = task

    async def infer(self, operation: Callable[[], T]) -> T:
        if not self.is_owner or asyncio.current_task() is not self._lease_task or self._pool is None:
            raise RuntimeError("Inference must be submitted by the active Store owner operation")
        submitted = time.monotonic()
        context = contextvars.copy_context()

        def execute() -> T:
            counters.bump("recall_inference_pending", -1)
            counters.bump("recall_inference_active", 1)
            counters.bump("recall_inference_calls")
            _observe_wait("recall_inference_queue_ms", (time.monotonic() - submitted) * 1000)
            try:
                with op_timer("recall_inference_ms"):
                    return context.run(operation)
            finally:
                counters.bump("recall_inference_active", -1)

        counters.bump("recall_inference_pending", 1)
        try:
            pending = asyncio.get_running_loop().run_in_executor(self._pool, execute)
        except Exception:
            counters.bump("recall_inference_pending", -1)
            raise
        return await self.suspend(pending)

    async def _execute_job(self, job: _Job[T]) -> None:
        self._lease_task = asyncio.current_task()
        try:
            with op_timer("store_owner_execute_ms"):
                value = await job.operation()
        except BaseException as exc:
            counters.bump("store_owner_failed")
            error = exc if isinstance(exc, Exception) else RuntimeError(f"Owner operation aborted: {exc}")
            job.result.set_exception(error)
            if not isinstance(exc, Exception):
                self.stop()
                raise error from exc
        else:
            job.result.set_result(value)
            counters.bump("store_owner_completed")
        finally:
            if asyncio.current_task() in self._lease_released:
                self._lease_released.discard(asyncio.current_task())
            else:
                self._lease_task = None
                self._lease.release()
            with self._condition:
                self._active -= 1

    async def _serve(self) -> None:
        running: set[asyncio.Task] = set()
        failure: BaseException | None = None
        while True:
            await self._lease.acquire()
            with self._condition:
                if self._closing:
                    self._lease.release()
                    break
                job = self._jobs.popleft() if self._jobs else None
                if job is not None:
                    counters.bump("store_owner_pending", -1)
                    self._active += 1
                else:
                    self._wake.clear()
            if job is None:
                self._lease.release()
                await self._wake.wait()
                continue
            if not job.result.set_running_or_notify_cancel():
                counters.bump("store_owner_cancelled_before_start")
                with self._condition:
                    self._active -= 1
                self._lease.release()
                continue
            _observe_wait("store_owner_wait_ms", (time.monotonic() - job.queued_at) * 1000)
            task = job.context.run(lambda: asyncio.create_task(self._execute_job(job)))
            running.add(task)
            # Keep only live tasks, while retrieving failures before discarding them.
            finished = {item for item in running if item.done()}
            for item in finished:
                error = item.exception()
                if error is not None:
                    failure = error
                    self.stop()
            running.difference_update(finished)
        if running:
            results = await asyncio.gather(*running, return_exceptions=True)
            failure = next((value for value in results if isinstance(value, BaseException)), failure)
        if failure is not None:
            raise failure

    def _run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        except Exception as exc:
            self.stop()
            self._ready.set_exception(exc)
            self._done.set_exception(exc)
            LOG.exception("Store owner event loop initialization failed")
            return
        failure: BaseException | None = None
        try:
            with op_timer("store_owner_bootstrap_ms"):
                loop.run_until_complete(self._bootstrap())
            self._loop = loop
            self._wake = asyncio.Event()
            self._lease = asyncio.Lock()
            self._pool = ThreadPoolExecutor(max_workers=self.settings.inference_workers,
                                            thread_name_prefix="tam-recall-inference")
            self._ready.set_result(None)
            loop.run_until_complete(self._serve())
        except BaseException as exc:
            failure = exc if isinstance(exc, Exception) else RuntimeError(f"Store owner stopped: {exc}")
            LOG.exception("Store owner failed")
            if not self._ready.done():
                self._ready.set_exception(failure)
        finally:
            self.stop()
            try:
                with op_timer("store_owner_cleanup_ms"):
                    loop.run_until_complete(self._cleanup())
                tasks = asyncio.all_tasks(loop)
                for task in tasks:
                    task.cancel()
                if tasks:
                    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
            except BaseException as exc:
                failure = RuntimeError(f"Store owner cleanup failed: {exc}")
                LOG.exception("Store owner cleanup failed")
            finally:
                if self._pool is not None:
                    self._pool.shutdown(wait=True)
                loop.close()
                if failure is None:
                    self._done.set_result(None)
                else:
                    self._done.set_exception(failure)
