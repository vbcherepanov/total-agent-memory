from __future__ import annotations

import os

from config import get_torch_threads
from memory_core.telemetry import counters, op_timer


def configure_torch_threads() -> None:
    threads = get_torch_threads()
    os.environ['OMP_NUM_THREADS'] = str(threads)
    os.environ['OPENBLAS_NUM_THREADS'] = str(threads)
    os.environ['MKL_NUM_THREADS'] = str(threads)
    os.environ['VECLIB_MAXIMUM_THREADS'] = str(threads)
    import torch
    from threadpoolctl import threadpool_limits

    with op_timer('torch_cpu_configure_ms'):
        torch.set_num_threads(threads)
        # Keep the process limit; per-request restoration races with other requests.
        threadpool_limits(limits=threads, user_api='blas')
        counters.bump('torch_cpu_configured')
