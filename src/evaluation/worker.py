from __future__ import annotations

from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
import time

from eval_harness import EvalHarness, _percentile
from evaluation.locomo import load_dataset
from memory_core.retrieval import flatten_results

FIXTURE_PROJECT = 'tam-benchmark-fixture'


def _deny_network(event: str, args: tuple[object, ...]) -> None:
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):
        raise OSError('Evaluation is offline; prepare local model caches before running')


def _search(recall, query: str, project: str, limit: int):
    result = recall.search(query, project, 'all', limit, 'full', record_usage=False)
    return flatten_results(result)


def _benchmark(store, recall, top_k: int, limit: int | None) -> dict:
    from file_context import FileContextGuard

    fixture = json.loads(Path(__file__).with_name('fixtures.json').read_text(encoding='utf-8'))
    store.session_start(FIXTURE_PROJECT, project=FIXTURE_PROJECT)
    for record in fixture['records']:
        store.save_knowledge(sid=FIXTURE_PROJECT, ktype=record['type'], content=record['content'],
                             project=FIXTURE_PROJECT, tags=record['tags'], skip_quality=True, skip_dedup=True)
    scenarios = [dict(s, project=FIXTURE_PROJECT, k=top_k) for s in fixture['scenarios']]
    if limit is not None:
        scenarios = scenarios[:limit]
    harness = EvalHarness(
        recall_fn=lambda query, params: _search(recall, query, params['project'], params['limit']),
        file_warnings_fn=lambda file, params: FileContextGuard(store.db).get_file_warnings(
            file, project=params['project']))
    return harness.run_suite(scenarios)


def _locomo(store, recall, path: str, top_k: int, limit: int | None) -> dict:
    conversations = load_dataset(path)
    rows = []
    latencies = []
    ranks = []
    remaining = limit
    for index, conversation in enumerate(conversations):
        if remaining == 0:
            break
        questions = conversation.questions if remaining is None else conversation.questions[:remaining]
        project = f'locomo-{index}'
        store.session_start(project, project=project)
        identities: dict[int, str] = {}
        for turn in conversation.turns:
            content = f'[{turn.session_date}] {turn.speaker}: {turn.text}'
            if turn.caption:
                content += f'\n(image: {turn.caption})'
            saved = store.save_knowledge(sid=project, ktype='fact', content=content, project=project,
                                         tags=[turn.dialogue_id, 'locomo'], skip_quality=True, skip_dedup=True)
            identities[saved[0]] = turn.dialogue_id
        for question in questions:
            start = time.perf_counter()
            hits = _search(recall, question.text, project, max(top_k, 10))
            latency = (time.perf_counter() - start) * 1000
            latencies.append(latency)
            retrieved = [identities[hit['id']] for hit in hits if hit['id'] in identities]
            scored = question.category != 5 and bool(question.evidence)
            rank = next((i for i, identity in enumerate(retrieved, 1) if identity in question.evidence), None)
            if scored:
                ranks.append(rank)
            rows.append({'sample_id': conversation.sample_id, 'question': question.text,
                         'category': question.category, 'evidence': list(question.evidence),
                         'retrieved': retrieved, 'scored': scored, 'first_match_rank': rank})
        if remaining is not None:
            remaining -= len(questions)
    if not ranks:
        raise ValueError('LoCoMo selection has no questions with scorable evidence; increase limit')
    return {'dataset': 'LoCoMo', 'scenarios_total': len(ranks),
            'scenarios_passed': sum(rank is not None and rank <= top_k for rank in ranks),
            'questions_evaluated': len(rows), 'questions_unscored': len(rows) - len(ranks),
            'recall_at_5': sum(rank is not None and rank <= 5 for rank in ranks) / len(ranks),
            'recall_at_10': sum(rank is not None and rank <= 10 for rank in ranks) / len(ranks),
            'latency_ms': _percentile(latencies, .95), 'rows': rows}


def main() -> int:
    try:
        request = json.load(sys.stdin)
        sys.addaudithook(_deny_network)
        with redirect_stdout(sys.stderr):
            import server
            from memory_core.telemetry import counters, op_timer

            with op_timer('isolated_evaluation_ms'):
                store = server.Store(background_queues=False)
                recall = server.Recall(store)
                try:
                    if request['kind'] == 'benchmark':
                        result = _benchmark(store, recall, request['top_k'], request['limit'])
                    elif request['kind'] == 'locomo':
                        result = _locomo(store, recall, request['dataset_path'], request['top_k'], request['limit'])
                    else:
                        raise ValueError('Unknown evaluation kind')
                    result['embedding_mode'] = store._embed_mode
                    result['mode'] = 'fast'
                    result['isolated'] = True
                    counters.bump('isolated_evaluation_completed')
                    snapshot = counters.snapshot()
                    result['llm_calls_during_eval'] = snapshot.get('llm_calls', 0)
                    result['network_calls_during_eval'] = snapshot.get('network_calls', 0)
                finally:
                    recall.close()
                    store.db.close()
        sys.stdout.write(json.dumps(result))
        return 0
    except Exception as error:
        sys.stdout.write(json.dumps({'error': f'{type(error).__name__}: {error}'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
