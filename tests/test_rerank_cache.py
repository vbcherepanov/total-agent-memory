from concurrent.futures import ThreadPoolExecutor

import pytest

from memory_core.rerank_cache import RerankScoreCache


def test_exact_input_only_and_private_result():
    cache = RerankScoreCache(20)
    calls = []

    def infer(query, texts):
        calls.append((query, texts))
        return [float(len(text)) for text in texts]

    first = cache.score('Query', ['a', 'bb'], infer)
    first[0] = 99
    assert cache.score('Query', ['a', 'bb'], infer) == [1, 2]
    assert cache.score('Query', ['bb', 'a'], infer) == [2, 1]
    for query, texts in [('query', ['a', 'bb']), ('Query', ['bb', 'a']),
                         ('Query', ['a', 'bc']), ('Query ', ['a', 'bb'])]:
        cache.score(query, texts, infer)
    assert len(calls) == 4


def test_eviction_is_bounded_by_scores_and_recent_use():
    cache = RerankScoreCache(4)
    calls = []

    def infer(query, texts):
        calls.append(query)
        return [1.0] * len(texts)

    for query in ['a', 'b', 'a', 'c', 'a', 'b']:
        cache.score(query, ['x', 'y'], infer)
    assert calls == ['a', 'b', 'c', 'b']
    assert cache.size == 4


@pytest.mark.parametrize('capacity', [0, 1])
def test_disabled_or_oversize_does_not_retain(capacity):
    cache = RerankScoreCache(capacity)
    calls = []

    def infer(query, texts):
        calls.append(query)
        return [1.0] * len(texts)

    for _ in range(2):
        assert cache.score('a', ['x', 'y'], infer) == [1, 1]
    assert len(calls) == 2
    assert cache.size == 0


def test_failed_or_incomplete_scores_are_not_cached():
    cache = RerankScoreCache(10)
    with pytest.raises(OSError):
        cache.score('a', ['x'], lambda *_: (_ for _ in ()).throw(OSError('failed')))
    with pytest.raises(RuntimeError, match='scores'):
        cache.score('a', ['x'], lambda *_: [])
    assert cache.size == 0
    assert cache.score('a', ['x'], lambda *_: [3.0]) == [3]


def test_concurrent_access_remains_bounded():
    cache = RerankScoreCache(10)

    def score(index):
        return cache.score(str(index), ['a', 'b'], lambda *_: [float(index), 1.0])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(score, range(40)))
    assert results == [[float(index), 1.0] for index in range(40)]
    assert cache.size <= 10


def test_key_has_unambiguous_boundaries():
    cache = RerankScoreCache(10)
    assert cache.score('a', ['bc'], lambda *_: [1.0]) == [1]
    assert cache.score('ab', ['c'], lambda *_: [2.0]) == [2]
    assert cache.score('a', ['b', 'c'], lambda *_: [3.0, 4.0]) == [3, 4]


def test_negative_capacity_rejected():
    with pytest.raises(ValueError):
        RerankScoreCache(-1)


@pytest.mark.parametrize('value', ['-1', 'invalid'])
def test_invalid_config_rejected(monkeypatch, value):
    from config import get_cross_rerank_cache_scores

    monkeypatch.setenv('MEMORY_CROSS_RERANK_CACHE_SCORES', value)
    with pytest.raises(ValueError):
        get_cross_rerank_cache_scores()


def test_cache_is_owned_by_loaded_model():
    first, second = RerankScoreCache(10), RerankScoreCache(10)
    assert first.score('q', ['a'], lambda *_: [1.0]) == [1]
    assert second.score('q', ['a'], lambda *_: [2.0]) == [2]


def test_reordered_duplicates_preserve_multiplicity():
    cache = RerankScoreCache(10)
    calls = []

    def infer(query, texts):
        calls.append(texts)
        return [float(len(text)) for text in texts]

    assert cache.score('q', ['a', 'bb', 'a'], infer) == [1, 2, 1]
    assert cache.score('q', ['bb', 'a', 'a'], infer) == [2, 1, 1]
    assert len(calls) == 1
    assert cache.score('q', ['bb', 'bb', 'a'], infer) == [2, 2, 1]
    assert len(calls) == 2


def test_cache_only_never_invokes_encoder_on_miss_or_eviction():
    import pytest
    from memory_core.rerank_cache import RerankScoreCache, ScoreCacheMiss

    cache = RerankScoreCache(2)
    calls = []
    def infer(query, texts):
        calls.append(query)
        return [float(len(text)) for text in texts]
    with pytest.raises(ScoreCacheMiss):
        cache.score('first', ['a', 'bb'], infer, cache_only=True)
    assert calls == []
    assert cache.score('first', ['a', 'bb'], infer) == [1, 2]
    assert cache.score('first', ['bb', 'a'], infer, cache_only=True) == [2, 1]
    cache.score('second', ['ccc'], infer)
    with pytest.raises(ScoreCacheMiss):
        cache.score('first', ['a', 'bb'], infer, cache_only=True)
    assert calls == ['first', 'second']
