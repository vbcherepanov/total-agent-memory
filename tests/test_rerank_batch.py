from __future__ import annotations

import pytest

from memory_core.rerank_batch import LengthBatchedEncoder


def test_grouping_restores_scores_and_preserves_duplicate_inputs():
    class Encoder:
        def rerank(self, query, documents, *, batch_size):
            assert query == 'question'
            assert documents == ['', 'a', 'a', 'bbb', 'longest']
            assert batch_size == 2
            return iter([0, 1, 1, 3, 7])

    result = LengthBatchedEncoder(Encoder(), 2).rerank('question', iter(['longest', 'a', '', 'bbb', 'a']))
    assert result == [7, 1, 0, 3, 1]


@pytest.mark.parametrize('scores', [[1], [1, 2, 3]])
def test_incorrect_score_count_is_rejected(scores):
    class Encoder:
        def rerank(self, *_args, **_kwargs):
            return scores

    with pytest.raises(RuntimeError, match='scores for 2 candidates'):
        LengthBatchedEncoder(Encoder(), 8).rerank('q', ['a', 'bb'])


def test_empty_input_does_not_invoke_encoder_and_errors_propagate():
    class Encoder:
        def rerank(self, *_args, **_kwargs):
            raise OSError('inference failed')

    encoder = LengthBatchedEncoder(Encoder(), 8)
    assert encoder.rerank('q', []) == []
    with pytest.raises(OSError, match='inference failed'):
        encoder.rerank('q', ['a'])


@pytest.mark.parametrize('batch_size', [0, -1])
def test_invalid_adapter_batch_size(batch_size):
    with pytest.raises(ValueError, match='positive'):
        LengthBatchedEncoder(None, batch_size)


def test_batch_configuration_and_shared_model_reload(monkeypatch):
    import config
    from memory_core import cross_rerank

    monkeypatch.setattr(cross_rerank, '_shared', None)
    monkeypatch.setenv('MEMORY_CROSS_RERANK', 'auto')
    monkeypatch.setenv('MEMORY_CROSS_RERANK_BATCH_SIZE', '8')
    first = cross_rerank.shared_reranker()
    assert cross_rerank.shared_reranker() is first
    monkeypatch.setenv('MEMORY_CROSS_RERANK_BATCH_SIZE', '0')
    assert cross_rerank.shared_reranker() is not first
    assert config.get_cross_rerank_batch_size() == 0
    monkeypatch.setenv('MEMORY_CROSS_RERANK_BATCH_SIZE', '-1')
    with pytest.raises(ValueError, match='zero or positive'):
        config.get_cross_rerank_batch_size()


@pytest.mark.parametrize('batch_size', [0, 8])
def test_default_loader_uses_configured_batch_adapter(monkeypatch, batch_size):
    from memory_core import fastembed_loader
    from memory_core.cross_rerank import CrossReranker

    encoder = object()
    calls = []
    def load(*args, **kwargs):
        calls.append(kwargs)
        return encoder
    monkeypatch.setattr(fastembed_loader, 'load_model', load)
    monkeypatch.setenv('MEMORY_CROSS_RERANK_THREADS', '2')
    monkeypatch.setenv('MEMORY_CROSS_RERANK_BATCH_SIZE', str(batch_size))
    loaded = CrossReranker._load_fastembed('test')
    assert calls == [{'threads': 2}]
    assert isinstance(loaded, LengthBatchedEncoder) if batch_size else loaded is encoder


@pytest.mark.parametrize('value', ['0', '-1', 'invalid'])
def test_invalid_cross_encoder_thread_budget(monkeypatch, value):
    import config
    monkeypatch.setenv('MEMORY_CROSS_RERANK_THREADS', value)
    with pytest.raises(ValueError):
        config.get_cross_rerank_threads()


def test_shared_encoder_changes_when_thread_budget_changes(monkeypatch):
    from memory_core import cross_rerank
    monkeypatch.setenv('MEMORY_CROSS_RERANK', 'on')
    monkeypatch.setattr(cross_rerank, '_shared', None)
    monkeypatch.setenv('MEMORY_CROSS_RERANK_THREADS', '1')
    first = cross_rerank.shared_reranker()
    monkeypatch.setenv('MEMORY_CROSS_RERANK_THREADS', '2')
    second = cross_rerank.shared_reranker()
    assert first is not second
    assert (first._threads, second._threads) == (1, 2)
