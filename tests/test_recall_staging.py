from __future__ import annotations

import asyncio
import threading

import pytest

import server
from memory_core.cross_rerank import CrossReranker
from memory_core.store_owner import OwnerOverloaded, OwnerSettings, StoreOwner
from tests.test_store_owner import nothing, wait_set

WAIT_SECONDS = 5


def test_inference_releases_owner_but_ordinary_await_keeps_it_serial():
    started, release = threading.Event(), threading.Event()
    threads = []

    def infer():
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(WAIT_SECONDS)
        return 7

    async def run():
        owner = StoreOwner(nothing, nothing, OwnerSettings())
        await owner.ready()
        async def slow():
            threads.append(threading.get_ident())
            result = await owner.infer(infer)
            threads.append(threading.get_ident())
            return result
        async def probe():
            return threading.get_ident()
        pending = asyncio.create_task(owner.call(slow))
        try:
            await wait_set(started)
            assert await asyncio.wait_for(owner.call(probe), WAIT_SECONDS) == threads[0]
            assert not pending.done()
            release.set()
            assert await pending == 7
            assert threads[0] == threads[2] != threads[1]
            entered = asyncio.Event()
            unblocked = asyncio.Event()
            async def normal():
                entered.set()
                await unblocked.wait()
            task = asyncio.create_task(owner.call(normal))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            queued = asyncio.create_task(owner.call(probe))
            await asyncio.sleep(0.02)
            assert not queued.done()
            owner._loop.call_soon_threadsafe(unblocked.set)
            await task
            await queued
        finally:
            release.set()
            await owner.close()
    asyncio.run(run())


def test_pool_and_total_admission_remain_bounded_and_shutdown_drains():
    started, release = threading.Event(), threading.Event()
    calls = []
    cleaned = []

    def infer():
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(WAIT_SECONDS)
        return len(calls)

    async def cleanup():
        cleaned.append(len(calls))

    async def run():
        owner = StoreOwner(nothing, cleanup, OwnerSettings(max_pending=1, inference_workers=1))
        await owner.ready()
        async def work():
            return await owner.infer(infer)
        first = asyncio.create_task(owner.call(work))
        try:
            await wait_set(started)
            second = asyncio.create_task(owner.call(work))
            await asyncio.sleep(0.02)
            assert len(calls) == 1
            with pytest.raises(OwnerOverloaded):
                await owner.call(nothing)
            owner.stop()
            release.set()
            await first
            await second
        finally:
            release.set()
            await owner.close()
        assert cleaned == [2]
    asyncio.run(run())


def test_inference_failure_reacquires_owner_and_preserves_context():
    async def run():
        owner = StoreOwner(nothing, nothing, OwnerSettings())
        await owner.ready()
        async def work():
            thread = threading.get_ident()
            def fail():
                raise ValueError('inference failed')
            with pytest.raises(ValueError, match='inference failed'):
                await owner.infer(fail)
            assert threading.get_ident() == thread
            return await owner.infer(lambda: 3)
        try:
            assert await owner.call(work) == 3
        finally:
            await owner.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutate', [False, True, 'external'])
def test_recall_stages_keep_ranking_and_restart_after_write(tmp_path, monkeypatch, mutate):
    from memory_core import cross_rerank
    monkeypatch.setattr(server, 'MEMORY_DIR', tmp_path)
    monkeypatch.setenv('MEMORY_RECALL_TOUCH_FLUSH_MS', '600000')
    monkeypatch.setenv('MEMORY_CROSS_RERANK', 'on')
    started, release = threading.Event(), threading.Event()
    calls = []
    holder = {}

    class Encoder:
        def rerank(self, query, documents):
            calls.append(threading.get_ident())
            started.set()
            assert release.wait(WAIT_SECONDS)
            return [float(len(text)) for text in documents]

    cross = CrossReranker('test', multilingual=True, window=10, weight=2, context_chars=100,
                         loader=lambda name: Encoder())
    assert cross.wait_ready(WAIT_SECONDS)
    monkeypatch.setattr(cross_rerank, 'shared_reranker', lambda: cross)

    async def bootstrap():
        store = server.Store()
        store._embed_mode = 'none'
        for identity, content in enumerate(('billing database is postgres', 'billing database uses replicas'), 1):
            store.db.execute("INSERT INTO knowledge(id,content,session_id,project,type,status,created_at,last_confirmed) "
                             "VALUES (?,?,'s','p','fact','active','2026-01-01','2026-01-01')", (identity, content))
        store.db.commit()
        holder.update(store=store, recall=server.Recall(store), owner_thread=threading.get_ident())

    async def cleanup():
        holder['recall'].close()
        holder['store'].db.close()

    async def run():
        owner = StoreOwner(bootstrap, cleanup, OwnerSettings())
        await owner.ready()
        async def search():
            return await holder['recall'].search_async(owner, 'billing database', project='p')
        task = asyncio.create_task(owner.call(search))
        try:
            await wait_set(started)
            if mutate:
                async def write():
                    if mutate == 'external':
                        import sqlite3
                        with sqlite3.connect(holder['store'].db_path) as other:
                            other.execute("UPDATE knowledge SET status='archived' WHERE id=2")
                    else:
                        holder['store'].db.execute("UPDATE knowledge SET status='archived' WHERE id=2")
                        holder['store'].db.commit()
                await owner.call(write)
            else:
                shared = asyncio.create_task(owner.call(search))
                await asyncio.sleep(0.02)
                async def other_diagnostics():
                    holder['store']._semantic_diagnostics = ['another request']
                await owner.call(other_diagnostics)
            release.set()
            result = await task
            if not mutate:
                second = await shared
                assert second == result and second is not result
                assert result['semantic_diagnostics'] != ['another request']
                assert all(count == 2 for count, _ in holder['recall'].usage_buffer()._pending.values())
                assert len(calls) == 2, 'one content and one neighbour inference, shared by both callers'
            async def serial():
                return holder['recall'].search('billing database', project='p', record_usage=False)
            reference = await owner.call(serial)
            assert result['results'] == reference['results']
            ids = [hit['id'] for group in result['results'].values() for hit in group]
            assert (2 not in ids) if mutate else (2 in ids)
            assert all(thread != holder['owner_thread'] for thread in calls)
        finally:
            release.set()
            await owner.close()
    asyncio.run(run())


def test_fatal_owner_job_waits_for_suspended_inference_before_cleanup():
    class FatalOperation(BaseException):
        pass
    started, release = threading.Event(), threading.Event()
    events = []
    def inference():
        started.set()
        assert release.wait(WAIT_SECONDS)
        events.append('inferred')
    async def cleanup():
        events.append('closed')
    async def run():
        owner = StoreOwner(nothing, cleanup, OwnerSettings())
        await owner.ready()
        async def work():
            await owner.infer(inference)
            events.append('finished')
        async def fatal():
            raise FatalOperation('fatal test')
        task = asyncio.create_task(owner.call(work))
        try:
            await wait_set(started)
            with pytest.raises(RuntimeError, match='Owner operation aborted'):
                await owner.call(fatal)
            assert events == []
            release.set()
            await task
            with pytest.raises(RuntimeError, match='Owner operation aborted'):
                await owner.close()
            assert events == ['inferred', 'finished', 'closed']
        finally:
            release.set()
    asyncio.run(run())


def test_flight_key_is_exact_on_query_and_ignores_record_usage():
    from memory_core.staged_search import SearchRequest

    assert SearchRequest('Foo').flight_key() != SearchRequest('foo').flight_key()
    assert SearchRequest('foo  bar').flight_key() != SearchRequest('foo bar').flight_key()
    assert SearchRequest('foo', record_usage=True).flight_key() == SearchRequest('foo', record_usage=False).flight_key()
