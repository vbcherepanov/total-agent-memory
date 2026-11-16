import asyncio
import contextvars
import json
import sqlite3
import threading
from dataclasses import replace

import pytest

from memory_core.store_owner import OwnerOverloaded, OwnerSettings, OwnerUnavailable, StoreOwner
from memory_core.telemetry import counters

# Deadline for a shutdown that is expected to succeed. Hosted Windows runners
# need well over 10 ms to run cleanup and stop the owner thread.
SHUTDOWN_GRACE_SECONDS = 10.0


async def nothing():
    return None


async def wait_set(event):
    for _ in range(1000):
        if event.is_set():
            return
        await asyncio.sleep(0.001)
    pytest.fail("owner did not reach synchronization point")


def test_connection_lifecycle_and_context_stay_on_owner():
    marker = contextvars.ContextVar("owner-test", default="unset")
    threads = []
    connections = []

    async def bootstrap():
        threads.append(threading.get_ident())
        connections.append(sqlite3.connect(":memory:"))
        connections[0].execute("CREATE TABLE entries (value TEXT)")

    async def cleanup():
        threads.append(threading.get_ident())
        connections[0].close()

    async def run():
        owner = StoreOwner(bootstrap, cleanup, OwnerSettings())
        await owner.ready()
        try:
            async def write():
                threads.append(threading.get_ident())
                value = marker.get()
                connections[0].execute("INSERT INTO entries VALUES (?)", (value,))
                marker.set("worker mutation")
                return value
            marker.set("first")
            assert await owner.call(write) == "first"
            marker.set("second")
            assert await owner.call(write) == "second"
            assert marker.get() == "second"
            with pytest.raises(sqlite3.ProgrammingError):
                connections[0].execute("SELECT 1")
        finally:
            await owner.close()
    asyncio.run(run())
    assert len(set(threads)) == 1
    assert threads[0] != threading.get_ident()


def test_bounded_queue_cancellation_and_heartbeat():
    started, release = threading.Event(), threading.Event()
    effects = []

    async def slow_write():
        started.set()
        assert release.wait(5)
        effects.append("once")

    async def queued_write():
        effects.append("cancelled")

    async def run():
        owner = StoreOwner(nothing, nothing, OwnerSettings(max_pending=1))
        await owner.ready()
        first = asyncio.create_task(owner.call(slow_write))
        try:
            await wait_set(started)
            queued = asyncio.create_task(owner.call(queued_write))
            await asyncio.sleep(0)
            with pytest.raises(OwnerOverloaded):
                await owner.call(nothing)
            queued.cancel()
            first.cancel()
            for task in (queued, first):
                with pytest.raises(asyncio.CancelledError):
                    await task
            replacement = asyncio.create_task(owner.call(nothing))
            await asyncio.sleep(0)
            assert not replacement.done(), "cancelled queued call must release its slot immediately"
            # The transport loop can still schedule work while the owner is blocked.
            ticks = []
            asyncio.get_running_loop().call_soon(ticks.append, "alive")
            await asyncio.sleep(0)
            assert ticks == ["alive"]
            release.set()
            await replacement
        finally:
            release.set()
            await owner.close()
        assert effects == ["once"]
    asyncio.run(run())


def test_shutdown_refuses_pending_and_new_calls_and_closes_after_running():
    started, release = threading.Event(), threading.Event()
    events = []

    async def work():
        started.set()
        assert release.wait(5)
        events.append("work")

    async def cleanup():
        events.append("closed")

    async def run():
        owner = StoreOwner(nothing, cleanup, OwnerSettings())
        await owner.ready()
        first = asyncio.create_task(owner.call(work))
        await wait_set(started)
        queued = asyncio.create_task(owner.call(nothing))
        await asyncio.sleep(0)
        owner.stop()
        try:
            with pytest.raises(OwnerUnavailable):
                await queued
            with pytest.raises(OwnerUnavailable):
                await owner.call(nothing)
        finally:
            release.set()
            await first
            await owner.close()
        assert events == ["work", "closed"]
    asyncio.run(run())


def test_bootstrap_failure_is_visible_and_cleanup_runs_on_owner():
    calls = []

    async def bootstrap():
        calls.append(threading.get_ident())
        raise ValueError("bootstrap failed")

    async def cleanup():
        calls.append(threading.get_ident())

    async def run():
        owner = StoreOwner(bootstrap, cleanup, OwnerSettings())
        with pytest.raises(ValueError, match="bootstrap failed"):
            await owner.ready()
        with pytest.raises(ValueError, match="bootstrap failed"):
            await owner.close()
        with pytest.raises(OwnerUnavailable):
            await owner.call(nothing)
    asyncio.run(run())
    assert len(calls) == 2 and len(set(calls)) == 1


def test_shutdown_timeout_leaves_cleanup_to_owner():
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    async def work():
        started.set()
        assert release.wait(5)

    async def cleanup():
        closed.set()

    async def run():
        owner = StoreOwner(nothing, cleanup, OwnerSettings(shutdown_seconds=0.01))
        await owner.ready()
        task = asyncio.create_task(owner.call(work))
        await wait_set(started)
        try:
            with pytest.raises(TimeoutError):
                await owner.close()
            assert not closed.is_set()
        finally:
            release.set()
            await task
            # The short deadline was only for the close that must time out.
            owner.settings = replace(owner.settings, shutdown_seconds=SHUTDOWN_GRACE_SECONDS)
            await owner.close()
        assert closed.is_set()
    asyncio.run(run())


def test_operation_error_does_not_stop_owner_and_metrics_are_bounded():
    async def fail():
        raise ValueError("bad operation")

    async def run():
        owner = StoreOwner(nothing, nothing, OwnerSettings())
        await owner.ready()
        try:
            with pytest.raises(ValueError, match="bad operation"):
                await owner.call(fail)
            assert await owner.call(nothing) is None
        finally:
            await owner.close()
    before = counters.snapshot()
    asyncio.run(run())
    after = counters.snapshot()
    assert after["store_owner_pending"] == before.get("store_owner_pending", 0)
    assert after["store_owner_wait_ms_count"] - before.get("store_owner_wait_ms_count", 0) == 2
    assert after["store_owner_execute_ms_bucket_le_inf"] - before.get("store_owner_execute_ms_bucket_le_inf", 0) == 2


@pytest.mark.parametrize("values", [{"TAM_STORE_MAX_PENDING": "0"}, {"TAM_STORE_MAX_PENDING": "bad"},
                                     {"TAM_STORE_SHUTDOWN_SECONDS": "nan"},
                                     {"TAM_STORE_SHUTDOWN_SECONDS": "0"}])
def test_invalid_settings_fail_before_thread_start(values):
    with pytest.raises(ValueError):
        OwnerSettings.from_env(values)


def test_cleanup_failure_is_visible():
    async def cleanup():
        raise ValueError("cannot close")

    async def run():
        owner = StoreOwner(nothing, cleanup, OwnerSettings())
        await owner.ready()
        with pytest.raises(RuntimeError, match="cannot close"):
            await owner.close()
    asyncio.run(run())


def test_personal_tool_raw_logs_and_provenance_use_owner(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "_STORE_OWNER", None)
    monkeypatch.setattr(server, "store", None)
    monkeypatch.setattr(server, "recall", None)
    monkeypatch.setattr(server, "TELEMETRY", None)
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("TAM_TOOL_TELEMETRY", "false")
    monkeypatch.setattr(server, "_v5_modules", {})
    visits = []
    original = server._dispatch_tool

    async def dispatch(name, args):
        visits.append(threading.get_ident())
        return await original(name, args)
    monkeypatch.setattr(server, "_dispatch_tool", dispatch)

    async def run():
        await server._bootstrap_session()
        try:
            main = threading.get_ident()
            with pytest.raises(sqlite3.ProgrammingError):
                server.store.db.execute("SELECT 1")
            content, failed = await server._call_tool_impl("memory_save", {
                "content": "Owner-thread regression record", "type": "fact", "project": "owner-test",
                "_ctx": {"agent": "codex", "session_id": "owner-provenance"}})
            assert not failed, content
            assert visits and visits[0] != main
            content, failed = await server._call_tool_impl("memory_recall", {
                "query": "Owner-thread regression", "project": "owner-test"})
            assert not failed, content
            assert len(set(visits)) == 1
            vectors = await server._prewarm_session("owner warmup")
            assert vectors
        finally:
            await server._shutdown_session()
        assert server.store is None
        content, failed = await server._call_tool_impl("memory_stats", {})
        assert failed and "not accepting" in content[0].text
    asyncio.run(run())
    assert list((tmp_path / "raw").glob("*.jsonl"))
    with sqlite3.connect(tmp_path / "memory.db") as db:
        assert db.execute("SELECT agent, session_id FROM memory_provenance WHERE tool='memory_save'").fetchall() == [
            ("codex", "owner-provenance")]


def test_http_health_responds_while_store_queue_is_busy(monkeypatch):
    import httpx
    import server
    import uvicorn

    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(server, "store", None)
    monkeypatch.setattr(server, "TELEMETRY", None)

    async def slow(name, args):
        started.set()
        assert release.wait(5)
        return json.dumps({"ok": True})
    monkeypatch.setattr(server, "_do", slow)

    class ProbeServer:
        def __init__(self, config):
            self.app = config.app

        async def serve(self, sockets=None):
            active = asyncio.create_task(server._call_tool_impl("memory_stats", {}))
            await wait_set(started)
            queued = asyncio.create_task(server._call_tool_impl("memory_stats", {}))
            await asyncio.sleep(0)
            try:
                content, error = await server._call_tool_impl("memory_stats", {})
                assert error and "queue is full" in content[0].text
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                             base_url="http://127.0.0.1") as client:
                    response = await asyncio.wait_for(client.get("/healthz"), 0.5)
                assert response.status_code == 200
                assert response.json()["status"] == "ok"
            finally:
                release.set()
                assert not (await active)[1]
                assert not (await queued)[1]

    monkeypatch.setattr(uvicorn, "Server", ProbeServer)

    async def run():
        owner = StoreOwner(nothing, nothing, OwnerSettings(max_pending=1))
        monkeypatch.setattr(server, "_STORE_OWNER", owner)
        await owner.ready()
        try:
            await server._run_streamable_http("127.0.0.1", 0)
        finally:
            await owner.close()
    asyncio.run(run())


def test_store_constructor_closes_connection_after_initialization_failure(monkeypatch, tmp_path):
    import server

    connections = []

    class Connection(sqlite3.Connection):
        def close(self):
            connections.append(threading.get_ident())
            super().close()

    def fail(_self):
        raise RuntimeError("failed services")

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(server.Store, "_init_services", fail)
    with pytest.raises(RuntimeError, match="failed services"):
        server.Store(connection_factory=Connection, thread_bound=True)
    assert connections == [threading.get_ident()]


def test_enrichment_contradictions_use_side_connection(monkeypatch):
    from types import SimpleNamespace

    import contradiction_detector
    import enrichment_worker
    from memory_core import vector_search

    db = object()
    seen = []

    class SideSearch:
        def __init__(self, connection, **kwargs):
            assert connection is db

        def search(self, vector, scope, **kwargs):
            seen.append(scope.project)
            return []

    def forbidden(*args, **kwargs):
        pytest.fail("main Store index used by enrichment worker")

    monkeypatch.setattr(vector_search, "VectorSearch", SideSearch)
    monkeypatch.setattr(contradiction_detector, "should_run", lambda kind: (True, ""))
    store = SimpleNamespace(thread_bound=True, embed=lambda texts: [[1.0, 0.0]], _binary_search=forbidden)
    task = SimpleNamespace(ktype="fact", content_snapshot="record", project="p", knowledge_id=1)
    enrichment_worker._run_contradiction_detector(db, task, store)
    assert seen == ["p"]


def test_error_journal_never_inspects_owner_connection_from_other_thread():
    from error_journal import ErrorJournal

    inspected = []

    class Connection:
        @property
        def in_transaction(self):
            inspected.append(threading.get_ident())
            return False

    journal = ErrorJournal(Connection())
    thread = threading.Thread(target=lambda: journal.log_error("embed", ValueError("broken"), "failed"))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert inspected == [], "foreign thread inspected owner's transaction"


def test_cancelled_bootstrap_eventually_cleans_up_on_owner():
    started, release, cleaned = threading.Event(), threading.Event(), threading.Event()

    async def bootstrap():
        started.set()
        assert release.wait(5)

    async def cleanup():
        cleaned.set()

    async def run():
        owner = StoreOwner(bootstrap, cleanup, OwnerSettings())
        ready = asyncio.create_task(owner.ready())
        await wait_set(started)
        ready.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ready
        owner.stop()
        release.set()
        await owner.close()
        assert cleaned.is_set()
    asyncio.run(run())


def test_owner_loop_creation_failure_is_reported(monkeypatch):
    original = asyncio.new_event_loop

    def create():
        if threading.current_thread().name == "tam-store-owner":
            raise OSError("no descriptors")
        return original()

    async def run():
        monkeypatch.setattr(asyncio, "new_event_loop", create)
        owner = StoreOwner(nothing, nothing, OwnerSettings())
        with pytest.raises(OSError, match="no descriptors"):
            await owner.ready()
        with pytest.raises(OSError, match="no descriptors"):
            await owner.close()
    asyncio.run(run())


def test_cancel_while_reacquiring_lease_does_not_release_foreign_lock():
    async def run():
        owner = StoreOwner(nothing, nothing, OwnerSettings())
        await owner.ready()
        blocker_errors = []

        async def operation():
            task = asyncio.current_task()
            loop = asyncio.get_running_loop()

            async def blocker():
                await owner._lease.acquire()  # takes the lease released by suspend()
                try:
                    task.cancel()  # interrupts the wait
                    await asyncio.sleep(0.05)  # task now blocks re-acquiring the lease
                    task.cancel()  # cancelled exactly at the lease acquire await
                    await asyncio.sleep(0.05)
                finally:
                    try:
                        owner._lease.release()
                    except RuntimeError as error:
                        blocker_errors.append(error)

            loop.create_task(blocker())
            await owner.suspend(loop.create_future())

        with pytest.raises(RuntimeError, match="Owner operation aborted"):
            await owner.call(operation)
        # A cancelled operation stops the owner; shutdown must surface only that abort,
        # not a "Lock is not acquired" error from releasing a lease the task never held.
        with pytest.raises(RuntimeError) as closed:
            await owner.close()
        assert "not acquired" not in str(closed.value)
        assert "not acquired" not in str(closed.value.__cause__ or "")
        assert not blocker_errors
        assert not owner._lease.locked()
    asyncio.run(run())
