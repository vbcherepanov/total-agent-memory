from concurrent.futures import ThreadPoolExecutor

import pytest

from memory_core import multilingual_rerank


@pytest.mark.parametrize('machine,expected', [
    ('arm64', multilingual_rerank.ARM_MODEL_FILE),
    ('aarch64', multilingual_rerank.ARM_MODEL_FILE),
    ('x86_64', multilingual_rerank.FLOAT_MODEL_FILE),
    ('AMD64', multilingual_rerank.FLOAT_MODEL_FILE),
])
def test_model_registration_is_portable_and_thread_safe(monkeypatch, machine, expected):
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    models = []
    calls = []

    def add(model, **kwargs):
        calls.append(kwargs)
        models.append({'model': model})

    monkeypatch.setattr(multilingual_rerank.platform, 'machine', lambda: machine)
    monkeypatch.setattr(TextCrossEncoder, 'list_supported_models', lambda: models)
    monkeypatch.setattr(TextCrossEncoder, 'add_custom_model', add)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: multilingual_rerank.register_model(), range(16)))
    assert len(calls) == 1
    assert calls[0]['model_file'] == expected
    assert calls[0]['sources'].hf == multilingual_rerank.MODEL


def test_default_reranks_russian_ukrainian_and_english(monkeypatch):
    from memory_core import cross_rerank

    monkeypatch.delenv('MEMORY_CROSS_RERANK_MODEL', raising=False)
    monkeypatch.setenv('MEMORY_CROSS_RERANK', 'on')
    monkeypatch.setattr(cross_rerank, '_shared', None)
    reranker = cross_rerank.shared_reranker()
    assert reranker.model == multilingual_rerank.MODEL
    assert all(reranker.applies_to(query) for query in ('размер очереди', 'розмір черги', 'queue size'))
