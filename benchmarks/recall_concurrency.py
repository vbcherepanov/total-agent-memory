"""Replay private recall arguments against an isolated database snapshot."""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

DEFAULT_CONCURRENCY = 8
RSS_SAMPLE_SECONDS = 0.02


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--concurrency', type=int, default=DEFAULT_CONCURRENCY)
    result = parser.parse_args()
    if result.concurrency < 1:
        parser.error('--concurrency must be positive')
    return result


async def replay(options: argparse.Namespace) -> None:
    import server
    from memory_core.cross_rerank import shared_reranker
    from memory_core.telemetry import counters

    cases = json.loads(options.cases.read_text(encoding='utf-8'))
    expected = None
    if options.reference:
        rows = json.loads(options.reference.read_text(encoding='utf-8'))
        expected = {row['case']: row['ranked_ids'] for row in rows if row['phase'] == 'first'}
    marker = contextvars.ContextVar('recall_profile_case', default=-1)
    stages: dict[int, dict[str, float]] = {}
    inference_stages: dict[int, dict[str, float]] = {}
    original = server._call_tool_on_owner

    async def timed(*args, **kwargs):
        index = marker.get()
        started = time.perf_counter()
        try:
            return await original(*args, **kwargs)
        finally:
            stages[index] = {'owner_started': started, 'execute_ms': (time.perf_counter() - started) * 1000}

    await server._bootstrap_session()
    try:
        cross = shared_reranker()
        if cross is None or not await asyncio.to_thread(cross.wait_ready):
            raise RuntimeError('Concurrency benchmark requires a loaded cross-encoder')
        (options.output / 'settings.json').write_text(json.dumps({
            'model': cross.model, 'window': cross.window, 'context_chars': cross.context_chars,
            'batch_size': cross._batch_size, 'encoder_threads': getattr(cross, '_threads', 'runtime-default'),
            'inference_workers': getattr(server._STORE_OWNER.settings, 'inference_workers', 0),
        }, indent=2), encoding='utf-8')
        server._call_tool_on_owner = timed
        owner = server._STORE_OWNER
        original_infer = getattr(owner, 'infer', None)
        if original_infer is not None:
            async def timed_infer(operation):
                index = marker.get()
                queued = time.perf_counter()
                def work():
                    started = time.perf_counter()
                    stage = inference_stages.setdefault(index, {'inference_queue_ms': 0.0, 'inference_ms': 0.0})
                    stage['inference_queue_ms'] += (started - queued) * 1000
                    try:
                        return operation()
                    finally:
                        stage['inference_ms'] += (time.perf_counter() - started) * 1000
                return await original_infer(work)
            owner.infer = timed_infer
        semaphore = asyncio.Semaphore(options.concurrency)

        async def invoke(index: int, case: dict) -> dict:
            async with semaphore:
                token = marker.set(index)
                started = time.perf_counter()
                try:
                    content, error = await server._call_tool_impl('memory_recall', case['args'])
                    elapsed = (time.perf_counter() - started) * 1000
                    if error:
                        raise RuntimeError(f'Recall case {index} failed')
                    payload = json.loads(content[0].text)
                    ids = [hit['id'] for group in payload.get('results', {}).values() for hit in group]
                    stage = stages[index]
                    return {'case': index, 'wall_ms': elapsed,
                            'queue_and_dispatch_ms': (stage['owner_started'] - started) * 1000,
                            'execute_ms': stage['execute_ms'],
                            **inference_stages.get(index, {}),
                            'ranking_equal': ids == expected[index] if expected is not None else None}
                finally:
                    marker.reset(token)

        counters.reset()
        rows = await asyncio.gather(*(invoke(index, case) for index, case in enumerate(cases)))
        (options.output / 'summary.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
        (options.output / 'metrics.json').write_text(json.dumps(counters.snapshot(), indent=2), encoding='utf-8')
        if expected is not None and not all(row['ranking_equal'] for row in rows):
            raise RuntimeError('Concurrent ranking differs from sequential reference; inspect summary.json')
    finally:
        server._call_tool_on_owner = original
        await server._shutdown_session()


def main() -> None:
    options = arguments()
    os.umask(0o077)
    options.output.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(options.database.resolve().as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(options.output / 'memory.db') as target:
            source.backup(target)
    os.environ['TAM_MEMORY_DIR'] = str(options.output.resolve())
    os.environ['MEMORY_LLM_ENABLED'] = 'false'
    os.environ['MEMORY_ASYNC_ENRICHMENT'] = 'false'
    sys.path.insert(0, str(options.source_root.resolve() / 'src'))
    import psutil
    process = psutil.Process()
    finished = threading.Event()
    peak_rss = [process.memory_info().rss]
    def sample() -> None:
        while not finished.wait(RSS_SAMPLE_SECONDS):
            peak_rss[0] = max(peak_rss[0], process.memory_info().rss)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        asyncio.run(replay(options))
    finally:
        finished.set()
        sampler.join()
        (options.output / 'resources.json').write_text(json.dumps({
            'peak_rss_bytes': peak_rss[0], 'concurrency': options.concurrency,
            'inference_workers': os.environ.get('TAM_RECALL_INFERENCE_WORKERS', 'default'),
            'rss_sample_seconds': RSS_SAMPLE_SECONDS,
        }, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
