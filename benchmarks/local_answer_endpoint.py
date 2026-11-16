from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from heldout_local_qa import CASES, FACTS, PROJECT, ROOT, normalize


async def run(args: argparse.Namespace) -> None:
    import server
    from ai_layer.grounded_reader import REFUSAL
    from memory_core.cross_rerank import shared_reranker

    async def call(name: str, values: dict) -> dict:
        content, error = await server._call_tool_impl(name, values)
        if error:
            raise RuntimeError(content[0].text)
        return json.loads(content[0].text)

    rows = []
    await server._bootstrap_session()
    try:
        for fact in FACTS:
            saved = await call('memory_save', {'content': fact, 'type': 'fact', 'project': PROJECT})
            if not saved.get('saved'):
                raise RuntimeError('Pilot fact was not saved')
        cross = shared_reranker()
        if cross is not None and not await asyncio.to_thread(cross.wait_ready):
            raise RuntimeError('Cross-encoder did not load')
        os.environ['MEMORY_LLM_ENABLED'] = 'true'
        for number in args.cases:
            case = CASES[number]
            row = {'case_index': number, 'case': asdict(case)}
            try:
                result = await call('memory_answer', {'query': case.question, 'project': PROJECT, 'limit': 5})
                answer = normalize(result['answer'])
                correct = (answer == normalize(REFUSAL) if not case.evidence
                           else any(normalize(alias) in answer for alias in case.answers))
                row.update(result=result, correct=correct)
            except Exception as exc:
                row.update(correct=False, error_type=type(exc).__name__, error=str(exc))
            rows.append(row)
            (args.output / 'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        summary = {'n': len(rows), 'correct': sum(row['correct'] for row in rows),
                   'errors': sum('error' in row for row in rows), 'model': args.model,
                   'case_indices': args.cases, 'endpoint': 'memory_answer', 'negative_retrieval': True,
                   'paid_calls': 0}
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    finally:
        await server._shutdown_session()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--cases', default='0,1,8,18,22,23')
    args = parser.parse_args()
    target = urlparse(args.url)
    if target.scheme != 'http' or target.hostname not in {'localhost', '127.0.0.1', '::1'}:
        parser.error('Only a local HTTP inference endpoint is allowed')
    args.cases = [int(number) for number in args.cases.split(',')]
    if not args.cases or any(not 0 <= number < len(CASES) for number in args.cases):
        parser.error('Invalid case indices')
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.update({'TAM_MEMORY_DIR': str(args.output / 'memory'), 'MEMORY_LLM_ENABLED': 'false',
                       'MEMORY_ASYNC_ENRICHMENT': 'false', 'MEMORY_QUALITY_GATE_ENABLED': 'false',
                       'MEMORY_CONTRADICTION_DETECT_ENABLED': 'false', 'MEMORY_NEGATIVE_RETRIEVAL': 'true',
                       'MEMORY_CONTRADICTION_SCORER': 'llm', 'MEMORY_LLM_PROVIDER': 'ollama',
                       'MEMORY_REASON_PROVIDER': 'ollama', 'MEMORY_REASON_MODEL': args.model,
                       'MEMORY_LLM_MODEL': args.model, 'OLLAMA_URL': args.url,
                       'MEMORY_LLM_FALLBACK_PROVIDERS': '', 'MEMORY_ALLOW_OLLAMA_IN_HOT_PATH': 'false'})
    sys.path.insert(0, str(ROOT / 'src'))
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
