from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Literal

DEFAULT_TIMEOUT_SECONDS = 3600


def run_isolated(kind: Literal['benchmark', 'locomo'], *, dataset_path: str | None = None,
                 top_k: int = 10, limit: int | None = None) -> dict[str, object]:
    if kind not in ('benchmark', 'locomo'):
        raise ValueError('Unknown evaluation kind')
    if kind == 'locomo' and not dataset_path:
        raise ValueError('LoCoMo requires dataset_path pointing to the downloaded locomo10.json dataset')
    if type(top_k) is not int or top_k < 1:
        raise ValueError('top_k must be a positive integer')
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError('limit must be a positive integer')
    timeout = float(os.environ.get('TAM_EVAL_TIMEOUT_SECONDS', DEFAULT_TIMEOUT_SECONDS))
    if not 0 < timeout <= DEFAULT_TIMEOUT_SECONDS:
        raise ValueError(f'TAM_EVAL_TIMEOUT_SECONDS must be in (0, {DEFAULT_TIMEOUT_SECONDS}]')
    source = str(Path(dataset_path).resolve()) if dataset_path else None
    with tempfile.TemporaryDirectory(prefix='tam-eval-') as directory:
        env = dict(os.environ)
        env.update({
            'TAM_MEMORY_DIR': directory, 'CLAUDE_MEMORY_DIR': directory,
            'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
            'MEMORY_LLM_ENABLED': 'false', 'MEMORY_MODE': 'fast',
            'MEMORY_USE_LLM_IN_HOT_PATH': 'false', 'MEMORY_ALLOW_OLLAMA_IN_HOT_PATH': 'false',
            'MEMORY_ASYNC_ENRICHMENT': 'false', 'MEMORY_ENRICHMENT_ENABLED': 'false',
            'MEMORY_EMBED_PROVIDER': 'fastembed', 'USE_OLLAMA_EMBED': 'false',
            'MEMORY_CROSS_RERANK': 'off', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
        })
        request = {'kind': kind, 'dataset_path': source, 'top_k': top_k, 'limit': limit}
        try:
            process = subprocess.run([sys.executable, '-m', 'evaluation.worker'],
                                     input=json.dumps(request), text=True, capture_output=True,
                                     env=env, cwd=directory, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f'Evaluation exceeded {timeout:g} seconds; reduce limit') from error
        try:
            result = json.loads(process.stdout)
        except ValueError as error:
            raise RuntimeError(f'Evaluation worker returned invalid output (exit {process.returncode})') from error
        if not isinstance(result, dict):
            raise RuntimeError('Evaluation worker returned a non-object result')
        if process.returncode:
            raise ValueError(str(result.get('error', 'Evaluation worker failed')))
        return result
