import os
import subprocess
import sys

import pytest


def test_cpu_budget_import_keeps_torch_optional():
    result = subprocess.run([sys.executable, '-c',
                             'import sys; import cpu_budget; assert "torch" not in sys.modules'],
                            env={**os.environ, 'PYTHONPATH': 'src'}, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('value', ['0', '-1', 'invalid'])
def test_invalid_cpu_budget_is_rejected(monkeypatch, value):
    from cpu_budget import configure_torch_threads
    monkeypatch.setenv('MEMORY_TORCH_THREADS', value)
    with pytest.raises(ValueError):
        configure_torch_threads()


@pytest.mark.parametrize('threads', [1, 2])
def test_real_torch_and_blas_obey_same_process_budget(threads):
    pytest.importorskip('torch')
    pytest.importorskip('threadpoolctl')
    code = '''
from cpu_budget import configure_torch_threads
configure_torch_threads()
import numpy as np
import torch
from threadpoolctl import threadpool_info, threadpool_limits
threadpool_limits(limits=4, user_api='blas')
left = torch.arange(256, dtype=torch.float32).reshape(16, 16) / 256
before = left @ left.T
configure_torch_threads()
after = left @ left.T
torch.testing.assert_close(before, after)
blas = [item for item in threadpool_info() if item['user_api'] == 'blas']
if not blas:
    config = np.show_config(mode='dicts')['Build Dependencies']['blas']
    assert __import__('sys').platform == 'darwin' and config['name'] == 'accelerate', config
    # Accelerate is not exposed by threadpoolctl; its startup limit is environmental.
    assert __import__('os').environ['VECLIB_MAXIMUM_THREADS'] == str(torch.get_num_threads())
assert all(item['num_threads'] == torch.get_num_threads() for item in blas), blas
assert torch.get_num_threads() == int(__import__('os').environ['MEMORY_TORCH_THREADS'])
assert __import__('os').environ['OMP_NUM_THREADS'] == str(torch.get_num_threads())
assert __import__('os').environ['OPENBLAS_NUM_THREADS'] == str(torch.get_num_threads())
assert __import__('os').environ['MKL_NUM_THREADS'] == str(torch.get_num_threads())
np.testing.assert_allclose(np.eye(4) @ np.eye(4), np.eye(4))
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=False,
                            env={**os.environ, 'PYTHONPATH': 'src', 'MEMORY_TORCH_THREADS': str(threads)}, timeout=60)
    assert result.returncode == 0, result.stderr
