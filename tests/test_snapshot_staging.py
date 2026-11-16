import asyncio
import threading

import pytest

from memory_core.snapshot_batch import SnapshotBatch
from memory_core.store_owner import OwnerSettings, StoreOwner
from tests.pg_store_support import store_backend  # noqa: F401
from tests.test_store_owner import wait_set

WAIT_SECONDS = 10
PROBE_SECONDS = 2


@pytest.mark.parametrize("mutate", [False, True])
def test_cold_snapshot_embedding_releases_owner_and_rechecks_revision(store_backend, tmp_path, monkeypatch, mutate):
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("MEMORY_CROSS_RERANK", "off")
    monkeypatch.setenv("MEMORY_RECALL_TOUCH_FLUSH_MS", "600000")
    entered, release = threading.Event(), threading.Event()
    calls = []
    sql_threads = []
    holder = {}
    original = SnapshotBatch.execute

    def controlled(batch):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            entered.set()
            assert release.wait(WAIT_SECONDS)
        return original(batch)

    monkeypatch.setattr(SnapshotBatch, "execute", controlled)

    async def bootstrap():
        store = server.Store(database=store_backend, background_queues=False)
        holder.update(store=store, recall=server.Recall(store), owner_thread=threading.get_ident())
        for content in ("Scheduler cobalt uses three retries", "Scheduler cobalt uses a bounded queue"):
            store.db.execute("INSERT INTO knowledge(session_id,type,content,project,created_at) "
                             "VALUES ('stage','fact',?,'snapshot-stage','2025-01-01T00:00:00Z')", (content,))
        store.db.commit()
        if not store.is_postgres:
            store.db.set_trace_callback(lambda _sql: sql_threads.append(threading.get_ident()))

    async def cleanup():
        holder["recall"].close()
        holder["store"].db.close()

    async def run():
        owner = StoreOwner(bootstrap, cleanup, OwnerSettings())
        await owner.ready()

        async def search():
            return await holder["recall"].search_async(owner, "Scheduler cobalt", project="snapshot-stage",
                                                      as_of="2025-01-02", record_usage=False)

        async def probe():
            db = holder["store"].db
            count = db.execute("SELECT count(*) FROM knowledge").fetchone()[0]
            if mutate:
                db.execute("UPDATE knowledge SET content='Scheduler cobalt corrected retry policy' WHERE id=1")
                db.commit()
            return count

        task = asyncio.create_task(owner.call(search))
        try:
            await wait_set(entered)
            assert await asyncio.wait_for(owner.call(probe), PROBE_SECONDS) == 2
            assert not task.done()
            release.set()
            result = await task

            async def reference():
                return holder["recall"].search("Scheduler cobalt", project="snapshot-stage", as_of="2025-01-02",
                                               record_usage=False)

            expected = await owner.call(reference)
            assert result["results"] == expected["results"]
            assert all(thread != holder["owner_thread"] for thread in calls)
            assert not sql_threads or set(sql_threads) == {holder["owner_thread"]}
            assert len(calls) >= (2 if mutate else 1)
        finally:
            release.set()
            await owner.close()

    asyncio.run(run())
