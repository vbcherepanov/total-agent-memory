"""Issue #43: save-path similarity lookup stays within the save latency budget on a 50k-record store.

The budget is the ``save_fast`` p95 ceiling of ``scripts/memory-perf-gate`` (50 ms); the similarity
lookup is only one slice of a save, so it must stay far below it. The store is built with bulk
inserts of synthetic records; a share of them starts and ends alike so the prefix/suffix indexes
return full candidate pages, the worst case for the bounded comparison.
"""
from __future__ import annotations

import os
import random
import statistics
import time

import pytest

from tests.pg_store_support import store_backend  # noqa: F401 -- fixture
from tests.test_dedup_updates import store  # noqa: F401 -- fixture

SCALE_ENV = 'TAM_RUN_SCALE_TESTS'
STORE_SIZE = 50_000
PROJECT = 'scale'
SAVE_P95_BUDGET_MS = 50.0
SAMPLES = 200
BULK_BATCH = 5_000
SEED = 43
VOCABULARY = tuple(f'word{index:04d}' for index in range(2_000))
WORDS_PER_RECORD = 24
CROWDED_SHARE = 5
CROWDED_PREFIX = 'the billing '
CROWDED_SUFFIX = ' in production.'


def _record(rng: random.Random, index: int) -> str:
    body = ' '.join(rng.choice(VOCABULARY) for _ in range(WORDS_PER_RECORD))
    if index % CROWDED_SHARE == 0:
        return f'{CROWDED_PREFIX}{body}{CROWDED_SUFFIX}'
    return f'{body} #{index}'


def _bulk_insert(store, rows: list[str]) -> None:
    for start in range(0, len(rows), BULK_BATCH):
        store.db.executemany(
            'INSERT INTO knowledge(session_id,type,content,project,created_at) VALUES (?,?,?,?,?)',
            [('scale', 'fact', text, PROJECT, '2026-11-15T00:00:00Z') for text in rows[start:start + BULK_BATCH]])
    store.db.commit()


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=100)[94]


@pytest.mark.skipif(os.environ.get(SCALE_ENV) != '1', reason=f'opt-in: set {SCALE_ENV}=1 (builds a 50k-record store, ~1 min per backend)')
def test_similarity_lookup_p95_on_50k_store(store, capsys):  # noqa: F811
    from memory_core.similar import find_similar

    rng = random.Random(SEED)
    rows = [_record(rng, index) for index in range(STORE_SIZE)]
    _bulk_insert(store, rows)
    assert store.db.execute('SELECT count(*) FROM knowledge WHERE project=?', [PROJECT]).fetchone()[0] == STORE_SIZE
    store.db.execute('ANALYZE')

    probes = [rows[rng.randrange(STORE_SIZE)] for _ in range(SAMPLES)]
    timings: list[float] = []
    found = 0
    for text in probes:
        variant = text[:-1] + ('x' if text[-1] != 'x' else 'y')
        started = time.perf_counter()
        report = find_similar(store.db, variant, PROJECT, 0)
        found += bool(report.matches)
        timings.append((time.perf_counter() - started) * 1000)
    p95 = _p95(timings)
    with capsys.disabled():
        print(f'\n[similarity {STORE_SIZE} rows] p50={statistics.median(timings):.2f}ms p95={p95:.2f}ms '
              f'max={max(timings):.2f}ms budget={SAVE_P95_BUDGET_MS}ms variants_found={found}/{SAMPLES}')
    assert found == SAMPLES
    assert p95 < SAVE_P95_BUDGET_MS
