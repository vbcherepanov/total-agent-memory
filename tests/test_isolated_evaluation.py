import json

import pytest

from evaluation.runner import run_isolated
from tests.test_locomo_dataset import sample


def test_packaged_benchmark_is_nonempty_and_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('TAM_MEMORY_DIR', str(tmp_path))
    sentinel = tmp_path / 'memory.db'
    sentinel.write_bytes(b'owner-memory-must-not-be-opened')
    result = run_isolated('benchmark')
    assert result['total'] == result['passed'] == 5, result
    assert sentinel.read_bytes() == b'owner-memory-must-not-be-opened'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['memory.db']


def test_real_locomo_evaluates_dialogue_evidence(tmp_path):
    path = tmp_path / 'locomo.json'
    path.write_text(json.dumps([sample()]), encoding="utf-8")
    result = run_isolated('locomo', dataset_path=str(path), limit=1)
    assert result['scenarios_total'] == result['scenarios_passed'] == 1, result
    assert result['recall_at_5'] == 1
    assert result['dataset'] == 'LoCoMo'
    assert result['llm_calls_during_eval'] == 0
    assert result['network_calls_during_eval'] == 0


def test_locomo_requires_dataset():
    with pytest.raises(ValueError, match='dataset_path'):
        run_isolated('locomo')


def test_worker_timeout_removes_temporary_store(monkeypatch):
    from pathlib import Path
    import subprocess
    from evaluation import runner
    paths = []
    def expire(command, **kwargs):
        path = Path(kwargs['env']['TAM_MEMORY_DIR'])
        assert path.is_dir()
        paths.append(path)
        raise subprocess.TimeoutExpired(command, 1)
    monkeypatch.setattr(runner.subprocess, 'run', expire)
    with pytest.raises(ValueError, match='exceeded'):
        run_isolated('benchmark')
    assert paths and not paths[0].exists()


@pytest.mark.parametrize('arguments', [{'limit': 0}, {'limit': -1}, {'top_k': 0}, {'top_k': True}])
def test_invalid_evaluation_limits(arguments):
    with pytest.raises(ValueError, match='positive integer'):
        run_isolated('benchmark', **arguments)
