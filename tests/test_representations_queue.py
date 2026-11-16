"""Tests for representations_queue — async multi-repr generation pipeline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def repq_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    root = Path(__file__).parent.parent
    conn.executescript((root / "migrations" / "001_v5_schema.sql").read_text(encoding="utf-8"))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT
        );
        """
    )
    conn.executescript((root / "migrations" / "002_multi_representation.sql").read_text(encoding="utf-8"))
    conn.executescript((root / "migrations" / "005_representations_queue.sql").read_text(encoding="utf-8"))
    yield conn
    conn.close()


def _add(db, content: str = "doc") -> int:
    return db.execute(
        "INSERT INTO knowledge (content, created_at) VALUES (?, ?)",
        (content, "2026-04-14T00:00:00Z"),
    ).lastrowid


def test_enqueue_idempotent(repq_db):
    from representations_queue import RepresentationsQueue

    q = RepresentationsQueue(repq_db)
    kid = _add(repq_db)
    assert q.enqueue(kid) is True
    assert q.enqueue(kid) is False
    cnt = repq_db.execute(
        "SELECT COUNT(*) FROM representations_queue WHERE knowledge_id=?", (kid,)
    ).fetchone()[0]
    assert cnt == 1


def test_process_writes_all_representations(repq_db):
    """Worker calls generator + embedder, writes one row per representation."""
    from representations_queue import RepresentationsQueue

    q = RepresentationsQueue(repq_db)
    kid = _add(repq_db, "Long content for generating four representations of.")
    q.enqueue(kid)

    def fake_generator(content: str, project: str | None = None) -> dict[str, str]:
        return {
            "summary": "short summary",
            "keywords": "alpha, beta, gamma",
            "questions": "Q1?\nQ2?",
        }

    def fake_embedder(text: str) -> list[float]:
        # Unique but deterministic embedding per text
        import hashlib
        h = hashlib.md5(text.encode()).digest()
        return [b / 255.0 for b in h[:8]]

    stats = q.process_pending(
        generator=fake_generator, embedder=fake_embedder, model_name="fake", limit=5
    )
    assert stats["processed"] == 1

    # Expected: 4 rows — raw + summary + keywords + questions
    rows = repq_db.execute(
        "SELECT representation, content FROM knowledge_representations "
        "WHERE knowledge_id=? ORDER BY representation",
        (kid,),
    ).fetchall()
    kinds = {r["representation"] for r in rows}
    assert kinds == {"raw", "summary", "keywords", "questions"}


def test_process_skips_empty_generator_output(repq_db):
    """If generator returns empty dict (e.g. all LLM calls failed), raw still stored."""
    from representations_queue import RepresentationsQueue

    q = RepresentationsQueue(repq_db)
    kid = _add(repq_db, "doc")
    q.enqueue(kid)

    stats = q.process_pending(
        generator=lambda c, **_: {"summary": "", "keywords": "", "questions": ""},
        embedder=lambda t: [0.1, 0.2, 0.3],
        model_name="fake",
        limit=1,
    )
    assert stats["processed"] == 1

    kinds = {
        r["representation"]
        for r in repq_db.execute(
            "SELECT representation FROM knowledge_representations WHERE knowledge_id=?",
            (kid,),
        ).fetchall()
    }
    # At minimum raw embedding stored
    assert "raw" in kinds
    # Empty generator outputs are skipped (no summary/keywords/questions written)
    assert "summary" not in kinds


def test_process_handles_generator_crash(repq_db):
    from representations_queue import RepresentationsQueue

    q = RepresentationsQueue(repq_db, max_attempts=2)
    kid = _add(repq_db, "doc")
    q.enqueue(kid)

    def boom(content, project=None):
        raise RuntimeError("ollama died")

    stats = q.process_pending(
        generator=boom,
        embedder=lambda t: [0.0] * 4,
        model_name="fake",
        limit=1,
    )
    assert stats["failed"] == 1

    row = repq_db.execute(
        "SELECT status FROM representations_queue WHERE knowledge_id=?", (kid,)
    ).fetchone()
    # Retryable
    assert row["status"] == "pending"


def test_stats(repq_db):
    from representations_queue import RepresentationsQueue

    q = RepresentationsQueue(repq_db)
    k1 = _add(repq_db, "a")
    k2 = _add(repq_db, "b")
    q.enqueue(k1)
    q.enqueue(k2)
    q.process_pending(
        generator=lambda c, **_: {"summary": "s"},
        embedder=lambda t: [0.1, 0.2],
        model_name="fake",
        limit=1,
    )
    s = q.stats()
    assert s["pending"] == 1
    assert s["done"] == 1


def test_stale_worker_cannot_ack_reclaimed_job(repq_db):
    from representations_queue import RepresentationsQueue

    first = RepresentationsQueue(repq_db)
    first.enqueue(_add(repq_db))
    old = first.claim_next()
    repq_db.execute("UPDATE representations_queue SET claimed_at='2000-01-01T00:00:00Z'")
    repq_db.commit()
    replacement = RepresentationsQueue(repq_db)
    current = replacement.claim_next()
    assert current['id'] == old['id']
    assert current['claim_token'] != old['claim_token']
    assert first.mark_done(old['id']) is False
    assert first.mark_failed(old['id'], 'late failure') is False
    assert replacement.mark_done(current['id']) is True


def test_retry_waits_instead_of_burning_attempts(repq_db):
    from representations_queue import RepresentationsQueue

    queue = RepresentationsQueue(repq_db)
    queue.enqueue(_add(repq_db))

    def fail(content):
        raise RuntimeError('provider unavailable')

    stats = queue.process_pending(fail, lambda text: [0.1, 0.2], 'fake', limit=10)
    assert stats['failed'] == 1
    row = repq_db.execute('SELECT attempts,next_attempt_at FROM representations_queue').fetchone()
    assert row['attempts'] == 1 and row['next_attempt_at']
    assert queue.claim_next() is None
    assert repq_db.execute('SELECT count(*) FROM knowledge_representations').fetchone()[0] == 0


def test_stale_generation_cannot_overwrite_new_worker(repq_db):
    from representations_queue import RepresentationsQueue

    queue = RepresentationsQueue(repq_db)
    queue.enqueue(_add(repq_db))

    def replaced(content):
        repq_db.execute("UPDATE representations_queue SET claimed_at='2000-01-01T00:00:00Z'")
        repq_db.commit()
        worker = RepresentationsQueue(repq_db)
        worker.process_pending(lambda text: {'summary': 'replacement'}, lambda text: [0.3, 0.4], 'fake')
        return {'summary': 'stale'}

    stats = queue.process_pending(replaced, lambda text: [0.1, 0.2], 'fake', limit=1)
    assert stats['skipped'] == 1
    assert repq_db.execute("SELECT content FROM knowledge_representations WHERE representation='summary'").fetchone()[0] == 'replacement'


def test_missing_embedding_is_retryable_not_done(repq_db):
    from representations_queue import RepresentationsQueue

    queue = RepresentationsQueue(repq_db)
    queue.enqueue(_add(repq_db))
    stats = queue.process_pending(lambda text: {}, lambda text: [], 'fake', limit=10)
    assert stats['failed'] == 1
    assert queue.stats()['pending'] == 1


def test_crash_budget_parks_job_as_failed(repq_db):
    from representations_queue import RepresentationsQueue

    queue = RepresentationsQueue(repq_db, max_attempts=1)
    queue.enqueue(_add(repq_db))
    queue.claim_next()
    repq_db.execute("UPDATE representations_queue SET claimed_at='2000-01-01T00:00:00Z'")
    repq_db.commit()
    assert queue.stats()['oldest_processing_seconds'] > 0
    assert queue.claim_next() is None
    assert queue.stats()['failed'] == 1


def test_legacy_representation_rebuild_is_bounded_and_resumable(repq_db):
    from representations_queue import RepresentationsQueue
    from memory_core.embedding_identity import EmbedIdentity

    queue = RepresentationsQueue(repq_db)
    for index in range(3):
        kid = _add(repq_db, f'doc {index}')
        queue.store.upsert(kid, 'summary', 'summary', [0.1, 0.2], 'old')
    assert queue.enqueue_incompatible(EmbedIdentity('new', 'declared'), limit=1) == 1
    assert queue.enqueue_incompatible(EmbedIdentity('new', 'declared'), limit=1) == 1
    assert queue.enqueue_incompatible(EmbedIdentity('new', 'declared'), limit=1) == 1
    assert queue.enqueue_incompatible(EmbedIdentity('new', 'declared')) == 0
    result = queue.process_pending(lambda text: {'summary': 'new'}, lambda text: [0.3, 0.4], 'new')
    assert result['processed'] == 3
    assert queue.enqueue_incompatible(EmbedIdentity('new', 'declared')) == 0


def test_legacy_views_recover_without_llm_or_metadata_changes(repq_db):
    from memory_core.embedding_identity import EmbedIdentity
    from multi_repr_search import search_with_winners
    from representations_queue import RepresentationsQueue

    repq_db.executescript('ALTER TABLE knowledge_representations ADD COLUMN parent_content_hash TEXT;'
                         'ALTER TABLE knowledge_representations ADD COLUMN last_confirmed TEXT;')
    queue = RepresentationsQueue(repq_db)
    kid = _add(repq_db, 'parent')
    for name in ('raw', 'summary', 'keywords', 'questions', 'compressed', 'criterion'):
        queue.store.upsert(kid, name, 'saved ' + name, [0.1, 0.2], 'old')
    repq_db.execute("UPDATE knowledge_representations SET embed_model='auto',embed_identity_key=NULL,"
                    "created_at='2001-01-01',last_confirmed='2002-01-01'")
    repq_db.commit()
    columns = 'id,knowledge_id,representation,content,created_at,parent_content_hash,last_confirmed'
    before = [tuple(row) for row in repq_db.execute('SELECT ' + columns + ' FROM knowledge_representations ORDER BY id')]
    identity = EmbedIdentity('new', 'declared')
    assert search_with_winners(repq_db, [0.3, 0.4], query_identity=identity)[0] == []
    assert queue.enqueue_incompatible(identity) == 1

    def no_llm(content):
        pytest.fail('Embedding repair must not regenerate existing text')

    result = queue.process_pending(no_llm, lambda text: [0.3, 0.4], 'new', identity_of=lambda: identity)
    assert result == {'processed': 1, 'failed': 0, 'skipped': 0, 'lost_claims': 0}
    after = [tuple(row) for row in repq_db.execute('SELECT ' + columns + ' FROM knowledge_representations ORDER BY id')]
    assert after == before
    hits, _ = search_with_winners(repq_db, [0.3, 0.4], query_identity=identity)
    assert [hit[0] for hit in hits] == [kid]
    assert queue.enqueue_incompatible(identity) == 0


def test_repair_provider_change_preserves_all_old_vectors(repq_db):
    from memory_core.embedding_identity import EmbedIdentity
    from representations_queue import RepresentationsQueue

    queue = RepresentationsQueue(repq_db)
    kid = _add(repq_db)
    queue.store.upsert(kid, 'summary', 'summary', [0.1, 0.2], 'old')
    queue.store.upsert(kid, 'keywords', 'keywords', [0.1, 0.2], 'old')
    before = [tuple(row) for row in repq_db.execute('SELECT * FROM knowledge_representations ORDER BY id')]
    queue.enqueue(kid)
    identity = EmbedIdentity('new', 'declared')

    def embed(text):
        nonlocal identity
        if text == 'keywords':
            identity = EmbedIdentity('fallback', 'declared')
        return [0.3, 0.4]

    result = queue.process_pending(lambda text: {}, embed, 'new', identity_of=lambda: identity)
    assert result['failed'] == 1
    assert [tuple(row) for row in repq_db.execute('SELECT * FROM knowledge_representations ORDER BY id')] == before


def test_repair_cannot_overwrite_a_concurrently_updated_view(repq_db):
    from representations_queue import RepresentationsQueue

    queue = RepresentationsQueue(repq_db)
    kid = _add(repq_db)
    queue.store.upsert(kid, 'summary', 'old summary', [0.1, 0.2], 'old')
    queue.enqueue(kid)

    def embed(text):
        if text == 'old summary':
            queue.store.upsert(kid, 'summary', 'new summary', [0.5, 0.6], 'new')
        return [0.3, 0.4]

    result = queue.process_pending(lambda text: {}, embed, 'new', limit=1)
    assert result['failed'] == 1
    assert queue.store.get_all_for(kid)[0]['content'] == 'new summary'


def test_lost_claim_failure_is_not_counted_as_failed(repq_db):
    from representations_queue import RepresentationsQueue

    queue = RepresentationsQueue(repq_db)
    queue.enqueue(_add(repq_db))
    thief = RepresentationsQueue(repq_db)

    def steal_then_fail(content):
        repq_db.execute("UPDATE representations_queue SET claimed_at='2000-01-01T00:00:00Z'")
        repq_db.commit()
        assert thief.claim_next() is not None
        raise RuntimeError('provider unavailable')

    stats = queue.process_pending(steal_then_fail, lambda text: [0.1, 0.2], 'fake', limit=1)
    assert stats['failed'] == 0
    assert stats['lost_claims'] == 1
    assert repq_db.execute('SELECT status FROM representations_queue').fetchone()['status'] == 'processing'
