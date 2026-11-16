from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from heldout_local_qa import CASES, FACTS, PROJECT, ROOT


async def evaluate(args: argparse.Namespace) -> None:
    import server
    from memory_core.cross_rerank import shared_reranker

    async def call(name: str, arguments: dict) -> dict:
        content, failed = await server._call_tool_impl(name, arguments)
        if failed:
            raise RuntimeError(f'Held-out tool failed: {name}')
        return json.loads(content[0].text)

    await server._bootstrap_session()
    try:
        ids = []
        for fact in FACTS:
            saved = await call('memory_save', {'content': fact, 'type': 'fact', 'project': PROJECT})
            if not saved.get('saved'):
                raise RuntimeError('Held-out fact was not saved')
            ids.append(saved['id'])
        cross = shared_reranker()
        if cross is None or not await asyncio.to_thread(cross.wait_ready):
            raise RuntimeError('Held-out staging check requires a loaded cross-encoder')
        semaphore = asyncio.Semaphore(args.concurrency)
        async def query(number, case):
            async with semaphore:
                result = await call('memory_recall', {'query': case.question, 'project': PROJECT, 'limit': 5})
                hits = [hit['id'] for group in result['results'].values() for hit in group]
                expected = {ids[index] for index in case.evidence}
                return {'case': number, 'answerable': bool(expected), 'all_evidence': expected <= set(hits),
                        'ranked_ids': hits}
        rows = await asyncio.gather(*(query(number, case) for number, case in enumerate(CASES)))
        summary = {'questions': len(rows), 'answerable': sum(row['answerable'] for row in rows),
                   'all_evidence': sum(row['answerable'] and row['all_evidence'] for row in rows),
                   'concurrency': args.concurrency, 'paid_calls': 0}
        if args.reference is not None:
            reference = json.loads(args.reference.read_text(encoding="utf-8"))
            summary['ranking_equal'] = sum(row['ranked_ids'] == other['ranked_ids']
                                           for row, other in zip(rows, reference, strict=True))
        (args.output / 'results.json').write_text(json.dumps(rows, indent=2), encoding="utf-8")
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding="utf-8")
        if summary['all_evidence'] != summary['answerable']:
            raise RuntimeError('Held-out retrieval lost required evidence')
        if 'ranking_equal' in summary and summary['ranking_equal'] != summary['questions']:
            raise RuntimeError('Held-out concurrent ranking differs from reference')
    finally:
        await server._shutdown_session()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--concurrency', type=int, default=8)
    parser.add_argument('--reference', type=Path)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error('Concurrency must be positive')
    args.output.mkdir(parents=True, exist_ok=False)
    corpus = json.dumps({'facts': FACTS, 'cases': [asdict(case) for case in CASES]},
                        ensure_ascii=False, sort_keys=True)
    (args.output / 'corpus.sha256').write_text(hashlib.sha256(corpus.encode()).hexdigest(), encoding="utf-8")
    os.environ.update({'TAM_MEMORY_DIR': str(args.output / 'memory'), 'MEMORY_LLM_ENABLED': 'false',
                       'MEMORY_CROSS_RERANK': 'on', 'MEMORY_ASYNC_ENRICHMENT': 'false',
                       'MEMORY_QUALITY_GATE_ENABLED': 'false', 'MEMORY_CONTRADICTION_DETECT_ENABLED': 'false'})
    sys.path.insert(0, str(args.source_root.resolve() / 'src'))
    asyncio.run(evaluate(args))


if __name__ == '__main__':
    main()
