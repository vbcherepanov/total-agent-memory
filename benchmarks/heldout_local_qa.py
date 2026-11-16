from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'heldout-local-20261115'
HTTP_TIMEOUT_SECONDS = 180
MAX_GENERATED_TOKENS = 96


@dataclass(frozen=True)
class Case:
    question: str
    answers: tuple[str, ...]
    evidence: tuple[int, ...]
    category: str


FACTS = (
    'The Vela Annex logistics coordinator is Marina Leontic.',
    'Marina Leontic assigned the Vela Annex delivery to dock Q73.',
    'The Vela Annex shipment must arrive on 18 February 2027.',
    'The backup contact for Vela Annex is Oleg Prentis.',
    'For the Moss Lantern workshop, the room reservation code is ML-6824.',
    'The Moss Lantern workshop starts at 14:35 on Thursday.',
    'The Moss Lantern workshop has a budget of 4170 euros.',
    'Moss Lantern participants must bring a yellow paper badge.',
    'Проект «Северная Лилия» координирует Ирина Вельская.',
    'Ирина Вельская назначила встречу проекта «Северная Лилия» в кабинете 516.',
    'Код доступа к материалам проекта «Северная Лилия» — NL-2947.',
    'Заказ для проекта «Северная Лилия» содержит 38 синих папок.',
    'The Copper Finch field team uses the radio channel 27.',
    'The Copper Finch field team leader is Daria Nestor.',
    'Daria Nestor designated Pine Landing as the Copper Finch rendezvous point.',
    'The Copper Finch equipment inventory contains 46 lanterns.',
    'На складі «Тиха Комета» відповідальна за приймання — Олена Вербова.',
    'Код комірки складу «Тиха Комета» — TC-8316.',
    'До складу «Тиха Комета» доставляють 72 зелені коробки.',
    'The Azure Kestrel review uses checklist revision AK-19.',
    'The Azure Kestrel reviewer is Petar Solnic.',
    'Petar Solnic scheduled the Azure Kestrel review for 09:20 on Tuesday.',
)
CASES = (
    Case('Who coordinates Vela Annex logistics?', ('Marina Leontic',), (0,), 'exact'),
    Case('Which dock receives the Vela Annex delivery?', ('Q73',), (1,), 'paraphrase'),
    Case('What is the arrival deadline for the Vela Annex shipment?', ('18 February 2027', '2027-02-18'), (2,), 'paraphrase'),
    Case('Who is the backup contact for Vela Annex?', ('Oleg Prentis',), (3,), 'exact'),
    Case('What room booking reference belongs to Moss Lantern?', ('ML-6824',), (4,), 'paraphrase'),
    Case('At what time does the Moss Lantern workshop start?', ('14:35',), (5,), 'exact'),
    Case('How much money is allocated to Moss Lantern?', ('4170', '4,170'), (6,), 'paraphrase'),
    Case('What color badge must Moss Lantern participants bring?', ('yellow',), (7,), 'exact'),
    Case('Who coordinates the Northern Lily project, Северная Лилия?', ('Ирина Вельская', 'Irina Velskaya'), (8,), 'cross_lingual'),
    Case('Which room hosts the Северная Лилия project meeting?', ('516',), (9,), 'cross_lingual'),
    Case('Какой код у материалов проекта Северная Лилия?', ('NL-2947',), (10,), 'exact'),
    Case('Сколько папок заказали для Северной Лилии?', ('38',), (11,), 'paraphrase'),
    Case('What radio channel does Copper Finch use?', ('27',), (12,), 'exact'),
    Case('Who leads the Copper Finch field team?', ('Daria Nestor',), (13,), 'exact'),
    Case('Where should the Copper Finch team assemble?', ('Pine Landing',), (14,), 'paraphrase'),
    Case('How many lanterns are in the Copper Finch inventory?', ('46',), (15,), 'exact'),
    Case('Who handles receiving at the Тиха Комета warehouse?', ('Олена Вербова', 'Olena Verbova'), (16,), 'cross_lingual'),
    Case('What is the storage cell code at Тиха Комета?', ('TC-8316',), (17,), 'cross_lingual'),
    Case('Скільки зелених коробок доставляють на склад Тиха Комета?', ('72',), (18,), 'exact'),
    Case('Which checklist revision does Azure Kestrel use?', ('AK-19',), (19,), 'exact'),
    Case('At what time did the Azure Kestrel reviewer schedule its review?', ('09:20',), (20, 21), 'multi_record'),
    Case('Which dock did the Vela Annex coordinator assign to that delivery?', ('Q73',), (0, 1), 'multi_record'),
    Case('What is the phone number of Marina Leontic?', ('unknown',), (), 'unanswerable'),
    Case('What is the passport number of the Copper Finch leader?', ('unknown',), (), 'unanswerable'),
)


def normalize(value: str) -> str:
    return ' '.join(re.findall(r'\w+', value.casefold()))


async def run(args: argparse.Namespace) -> None:
    import server
    from memory_core.cross_rerank import shared_reranker

    async def call(name: str, values: dict) -> dict:
        content, error = await server._call_tool_impl(name, values)
        if error:
            raise RuntimeError(f'Benchmark tool failed: {name}')
        return json.loads(content[0].text)

    ids = []
    await server._bootstrap_session()
    try:
        for fact in FACTS:
            result = await call('memory_save', {'content': fact, 'type': 'fact', 'project': PROJECT})
            if not result.get('saved'):
                raise RuntimeError('Held-out fact was not saved')
            ids.append(result['id'])
        cross = shared_reranker()
        if cross is not None and not await asyncio.to_thread(cross.wait_ready):
            raise RuntimeError('Configured cross-encoder did not load')
        rows = []
        with httpx.Client(base_url=args.url, timeout=HTTP_TIMEOUT_SECONDS, trust_env=False) as client:
            catalog = client.get('/api/tags')
            catalog.raise_for_status()
            model_digest = next(model['digest'] for model in catalog.json()['models'] if model['name'] == args.model)
            for case in CASES:
                result = await call('memory_recall', {'query': case.question, 'project': PROJECT, 'limit': 5})
                hits = [hit for group in result.get('results', {}).values() for hit in group]
                context = [{'id': hit['id'], 'text': hit['content']} for hit in hits]
                evidence = {ids[index] for index in case.evidence}
                row = {'case': asdict(case), 'retrieval_all_evidence': evidence <= {hit['id'] for hit in hits},
                       'retrieved_ids': [hit['id'] for hit in hits]}
                for mode, records in (('closed_book', []), ('with_memory', context)):
                    response = client.post('/api/chat', json={
                        'model': args.model, 'stream': False, 'format': 'json',
                        'options': {'temperature': 0, 'num_predict': MAX_GENERATED_TOKENS, 'num_ctx': 4096},
                        'messages': [
                            {'role': 'system', 'content': 'Answer only from the supplied records. If the answer is absent, '
                             'answer unknown. Return JSON with answer (short string) and ids (supporting record IDs).'},
                            {'role': 'user', 'content': json.dumps({'question': case.question, 'records': records},
                                                                  ensure_ascii=False)},
                        ],
                    })
                    response.raise_for_status()
                    raw = response.json()['message']['content']
                    try:
                        output = json.loads(raw)
                    except json.JSONDecodeError:
                        output = {'answer': '', 'ids': [], 'error': 'invalid_json', 'raw': raw}
                    if not isinstance(output, dict):
                        output = {'answer': '', 'ids': [], 'error': 'invalid_shape', 'raw': raw}
                    answer = normalize(str(output.get('answer', '')))
                    correct = (answer == 'unknown' if not case.evidence
                               else any(normalize(alias) in answer for alias in case.answers))
                    cited = output.get('ids', [])
                    grounded = (correct and isinstance(cited, list) and set(cited) <= {hit['id'] for hit in records}
                                and (evidence <= set(cited) if evidence else not cited))
                    row[mode] = {'answer': output, 'correct': correct, 'grounded': grounded}
                rows.append(row)
                (args.output / 'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        summary = {'n': len(rows), 'model': args.model, 'model_digest': model_digest,
                   'retrieval_all_evidence': sum(row['retrieval_all_evidence'] for row in rows if row['case']['evidence']),
                   'answerable': sum(bool(row['case']['evidence']) for row in rows),
                   'closed_book_correct': sum(row['closed_book']['correct'] for row in rows),
                   'with_memory_correct': sum(row['with_memory']['correct'] for row in rows),
                   'with_memory_grounded': sum(row['with_memory']['grounded'] for row in rows)}
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    finally:
        await server._shutdown_session()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    target = urlparse(args.url)
    if target.scheme != 'http' or target.hostname not in {'localhost', '127.0.0.1', '::1'}:
        parser.error('Only a local HTTP inference endpoint is allowed')
    args.output.mkdir(parents=True, exist_ok=False)
    known = json.loads((ROOT / 'tests/fixtures/recall_quality/corpus.json').read_text(encoding='utf-8'))
    if {normalize(case.question) for case in CASES} & {normalize(case['query']) for case in known['queries']}:
        raise RuntimeError('Held-out questions overlap the existing recall gate')
    corpus = json.dumps({'facts': FACTS, 'cases': [asdict(case) for case in CASES]}, ensure_ascii=False, sort_keys=True)
    (args.output / 'corpus.json').write_text(corpus, encoding='utf-8')
    (args.output / 'corpus.sha256').write_text(hashlib.sha256(corpus.encode()).hexdigest(), encoding='utf-8')
    os.environ.update({'TAM_MEMORY_DIR': str(args.output / 'memory'), 'MEMORY_LLM_ENABLED': 'false',
                       'MEMORY_ASYNC_ENRICHMENT': 'false', 'MEMORY_QUALITY_GATE_ENABLED': 'false',
                       'MEMORY_CONTRADICTION_DETECT_ENABLED': 'false'})
    sys.path.insert(0, str(ROOT / 'src'))
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
