import pytest

import server
from memory_core.embedding_identity import EmbedIdentity
from representations_queue import RepresentationsQueue
from tests.pg_store_support import store_backend  # noqa: F401


@pytest.fixture
def repair_store(store_backend, tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'MEMORY_DIR', tmp_path)
    store = server.Store(database=store_backend, background_queues=False)
    store.session_start('repair-session', project='repair')
    store.save_knowledge(sid='repair-session', content='parent', ktype='fact', project='repair')
    try:
        yield store
    finally:
        store.db.close()


def test_repair_preserves_saved_views_on_each_backend(repair_store):
    db = repair_store.db
    kid = db.execute("SELECT id FROM knowledge WHERE project='repair'").fetchone()[0]
    queue = RepresentationsQueue(db)
    for name in ('summary', 'keywords', 'questions'):
        queue.store.upsert(kid, name, 'saved ' + name, [0.1, 0.2], 'old')
    identity = EmbedIdentity('new', 'declared')
    queue.enqueue_incompatible(identity)

    def no_llm(text):
        pytest.fail('Repair must reuse saved text')

    stats = queue.process_pending(no_llm, lambda text: [0.3, 0.4], 'new', identity_of=lambda: identity)
    assert stats == {'processed': 1, 'failed': 0, 'skipped': 0, 'lost_claims': 0}
    assert {row.content for row in queue.store.snapshots(kid)} == {
        'saved summary', 'saved keywords', 'saved questions',
    }
    assert all(row.embed_identity_key == identity.key(2) for row in queue.store.snapshots(kid))


def test_reclaimed_repair_cannot_overwrite_replacement_worker(repair_store):
    db = repair_store.db
    kid = db.execute("SELECT id FROM knowledge WHERE project='repair'").fetchone()[0]
    queue = RepresentationsQueue(db)
    queue.store.upsert(kid, 'summary', 'saved summary', [0.1, 0.2], 'old')
    queue.enqueue(kid)
    replaced = False

    def embed(text):
        nonlocal replaced
        if text == 'saved summary' and not replaced:
            replaced = True
            db.execute("UPDATE representations_queue SET claimed_at='2000-01-01T00:00:00Z'")
            db.commit()
            worker = RepresentationsQueue(db)
            result = worker.process_pending(lambda content: {}, lambda content: [0.8, 0.9], 'replacement', limit=1)
            assert result['processed'] == 1
        return [0.3, 0.4]

    result = queue.process_pending(lambda text: {}, embed, 'new', limit=1)
    assert result['skipped'] == 1
    assert queue.store.snapshots(kid)[0].embed_model == 'replacement'
