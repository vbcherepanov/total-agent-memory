"""Gateway cross-scope re-rank: merge order, fallback, authorisation, single-scope equivalence."""
import asyncio
import time

import pytest

from memory_core.cross_rerank import CrossReranker
from team_memory.contracts import Forbidden, Scope, Work
from team_memory.registry import Registry
from team_memory.rerank import GatewayReranker
from team_memory.service import MemoryService
from tests.pg_store_support import store_backend  # noqa: F401 — fixture

WINDOW = 4


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class WordModel:
    """Scores a text by how many query words it contains; optionally slow or broken."""

    def __init__(self, delay=0.0, fail=False, on_call=None):
        self.delay, self.fail, self.on_call, self.calls = delay, fail, on_call, 0

    def rerank(self, query, texts):
        self.calls += 1
        if self.on_call:
            self.on_call()
        if self.fail:
            raise RuntimeError('encoder crashed')
        time.sleep(self.delay)
        words = set(query.lower().split())
        return [float(sum(word in words for word in text.lower().split())) for text in texts]


def reranker(model=None, window=WINDOW, loader=None):
    encoder = CrossReranker('fake', multilingual=True, window=window, weight=2.0, context_chars=0,
                            loader=loader or (lambda _name: model))
    return GatewayReranker(encoder_factory=lambda: encoder, wait_for_model=lambda: True), encoder


class FusedPool:
    """Pool double returning a deferred fused window per workspace (best fused first)."""

    timeout = 5.0

    def __init__(self, windows):
        self.windows, self.asked = windows, []

    def search_order(self, scopes):
        return scopes

    def invoke(self, work: Work, credential):
        self.asked.append((work.workspace.key, dict(work.arguments)))
        return [dict(record, fused_rank=rank) for rank, record in enumerate(self.windows[work.workspace.key])]


def record(record_id, content, score, rrf):
    return {'id': record_id, 'content': content, 'score': score, 'rrf_score': rrf, 'created_at': f'2026-01-0{record_id}',
            'rerank_context': 'context ' + content}


@pytest.fixture
def registry(tmp_path):
    result = Registry(tmp_path)
    result.add_user('anna', 'Anna')
    for team in ('sales', 'hr'):
        result.add_team(team, team)
    result.membership('anna', 'sales', 'reader')
    return result


def keys(registry, actor):
    return {w.scope.kind.value if w.scope.team_id is None else w.scope.team_id: w.key for w in registry.workspaces(actor)}


def windows_for(registry, token):
    key = keys(registry, registry.authenticate(token))
    return {
        key['personal']: [record(1, 'personal lunch note', 0.9, 0.03), record(2, 'personal gym note', 0.8, 0.02)],
        key['sales']: [record(1, 'discount rules general', 0.7, 0.03), record(2, 'discount approval limit north', 0.6, 0.02),
                       record(3, 'other sales text', 0.5, 0.01)],
        key['shared']: [record(1, 'cafeteria hours', 0.95, 0.03)],
    }


@pytest.mark.anyio
async def test_merged_window_is_reranked_once_and_kept_records_are_ordered_by_score(registry):
    token = registry.issue_token('anna', 'test')
    pool = FusedPool(windows_for(registry, token))
    gateway, _ = reranker(WordModel())
    service = MemoryService(registry, pool, reranker=gateway)
    result = await service.call(token, 'memory_recall', {'query': 'discount approval limit', 'limit': 2})
    assert all(arguments.get('defer_cross_rerank') is True for _, arguments in pool.asked)
    assert result['ordering'] == 'cross_rerank_then_score' and result['rerank'] == 'applied'
    # merged fused window (rank, then rrf, then scope order): personal#1, sales#1, shared#1, personal#2.
    # The encoder puts sales#1 first; the fused position keeps personal#1 second; shared#1 drops out.
    # The two kept records are then ordered by score.
    contents = [item['record']['content'] for item in result['results']]
    assert contents == ['personal lunch note', 'discount rules general']
    assert all('rerank_context' not in item['record'] and 'fused_rank' not in item['record'] for item in result['results'])
    assert gateway.counts['applied'] == 1


@pytest.mark.anyio
@pytest.mark.parametrize('window,second', [(4, 'discount rules general'), (10, 'discount approval limit north')])
async def test_only_records_inside_the_window_can_be_pulled_up(registry, window, second):
    token = registry.issue_token('anna', 'test')
    gateway, _ = reranker(WordModel(), window=window)
    service = MemoryService(registry, FusedPool(windows_for(registry, token)), reranker=gateway)
    result = await service.call(token, 'memory_recall', {'query': 'approval limit north', 'limit': 2})
    assert [item['record']['content'] for item in result['results']] == ['personal lunch note', second]


@pytest.mark.anyio
@pytest.mark.parametrize('case,expected', [('not_ready', 'not_ready'), ('failed', 'failed'), ('timeout', 'timeout')])
async def test_fallback_keeps_the_merged_fused_order(registry, case, expected):
    token = registry.issue_token('anna', 'test')
    pool = FusedPool(windows_for(registry, token))
    if case == 'not_ready':
        gateway, _ = reranker(loader=lambda _name: (_ for _ in ()).throw(OSError('model missing')))
    else:
        gateway, _ = reranker(WordModel(fail=case == 'failed', delay=0.5 if case == 'timeout' else 0.0))
        pool.timeout = 0.1
    service = MemoryService(registry, pool, reranker=gateway)
    result = await service.call(token, 'memory_recall', {'query': 'discount approval limit', 'limit': 3})
    assert result['rerank'] == expected
    # fused window order personal#1, sales#1, shared#1 -> kept, then ordered by score
    assert [item['record']['content'] for item in result['results']] == [
        'cafeteria hours', 'personal lunch note', 'discount rules general']


@pytest.mark.anyio
async def test_rerank_off_keeps_the_previous_merge(registry):
    token = registry.issue_token('anna', 'test')
    pool = FusedPool(windows_for(registry, token))
    service = MemoryService(registry, pool, reranker=GatewayReranker(encoder_factory=lambda: None))
    result = await service.call(token, 'memory_recall', {'query': 'discount', 'limit': 3})
    assert result['ordering'] == 'scope_rank_then_score' and 'rerank' not in result
    assert all('defer_cross_rerank' not in arguments for _, arguments in pool.asked)


@pytest.mark.anyio
async def test_merged_rerank_only_sees_workspaces_the_caller_can_read(registry):
    token = registry.issue_token('anna', 'test')
    windows = windows_for(registry, token)
    windows[registry.team_workspace_key('hr')] = [record(1, 'hr salary band discount approval limit', 9.0, 0.9)]
    pool = FusedPool(windows)
    gateway, _ = reranker(WordModel(), window=50)
    service = MemoryService(registry, pool, reranker=gateway)
    result = await service.call(token, 'memory_recall', {'query': 'salary band discount approval limit', 'limit': 50})
    readable = set(keys(registry, registry.authenticate(token)).values())
    assert {key for key, _ in pool.asked} == readable
    assert all('salary' not in item['record']['content'] for item in result['results'])
    assert {item['scope']['team_id'] or item['scope']['kind'] for item in result['results']} <= {'personal', 'sales', 'shared'}
    with pytest.raises(Forbidden):
        await service.call(token, 'memory_recall', {'query': 'salary', 'scope': {'kind': 'team', 'team_id': 'hr'}})


@pytest.mark.anyio
async def test_access_removed_during_the_rerank_withholds_the_results(registry):
    token = registry.issue_token('anna', 'test')
    model = WordModel(on_call=lambda: registry.membership('anna', 'sales', None))
    gateway, _ = reranker(model)
    service = MemoryService(registry, FusedPool(windows_for(registry, token)), reranker=gateway)
    with pytest.raises(Forbidden):
        await service.call(token, 'memory_recall', {'query': 'discount approval', 'limit': 3})
    assert model.calls == 1


@pytest.mark.anyio
async def test_single_scope_result_is_unchanged_by_moving_the_rerank_to_the_gateway(
        store_backend, registry, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    """The same store searched the old way (re-rank inside the worker) and the new way gives the same list."""
    import config
    import server
    from memory_core import cross_rerank
    from team_memory.contracts import Save
    from team_memory.worker import Runtime

    data_dir = tmp_path / 'runtime'
    monkeypatch.setattr(server, 'MEMORY_DIR', data_dir)
    for key, value in {'TAM_MEMORY_DIR': str(data_dir), 'CLAUDE_MEMORY_DIR': str(data_dir),
                       'MEMORY_QUALITY_GATE_ENABLED': 'false', 'MEMORY_ASYNC_ENRICHMENT': 'false',
                       'USE_BINARY_SEARCH': 'true', 'MEMORY_CROSS_RERANK': 'on'}.items():
        monkeypatch.setenv(key, value)
    encoder = CrossReranker(config.get_cross_rerank_model(), multilingual=True, window=config.get_cross_rerank_window(),
                            weight=config.get_cross_rerank_weight(), context_chars=config.get_cross_rerank_context(),
                            loader=lambda _name: WordModel())
    monkeypatch.setattr(cross_rerank, '_shared', encoder)
    runtime = Runtime(str(data_dir), store_backend)
    actor = registry.authenticate(token := registry.issue_token('anna', 'test'))
    workspace = registry.authorize(actor, Scope(), True)
    topics = ('release checklist', 'discount approval', 'office hours', 'vault rotation', 'salary review')
    try:
        for i in range(40):
            content = f'Note {i}: the {topics[i % 5]} step {i % 7} belongs to runbook {i % 3}.'
            runtime.execute(Work(actor=actor, workspace=workspace, operation='memory_save',
                                 arguments=Save(content=content).model_dump(mode='json', exclude={'scope'})))

        class RuntimePool:
            timeout = 30.0

            def search_order(self, scopes):
                return scopes

            def invoke(self, work, credential):
                return runtime.execute(work)

        gateway = GatewayReranker()
        new = MemoryService(registry, RuntimePool(), reranker=gateway)
        for query in ('discount approval step 3', 'vault rotation runbook 2', 'salary review note 12'):
            old = runtime.execute(Work(actor=actor, workspace=workspace, operation='memory_recall',
                                       arguments={'query': query, 'limit': 5, 'project': None}))
            result = await new.call(token, 'memory_recall', {'query': query, 'limit': 5, 'scope': {'kind': 'personal'}})
            assert result['rerank'] == 'applied'
            assert [item['record']['id'] for item in result['results']] == [item['id'] for item in old]
        loads = []
        untouched = CrossReranker(encoder.model, multilingual=True, window=encoder.window, weight=encoder.weight,
                                  context_chars=encoder.context_chars, loader=lambda name: loads.append(name))
        monkeypatch.setattr(cross_rerank, '_shared', untouched)
        window = runtime.execute(Work(actor=actor, workspace=workspace, operation='memory_recall', arguments={
            'query': 'discount approval step 3', 'limit': 5, 'project': None, 'defer_cross_rerank': True}))
        assert len(window) > 5 and [item['fused_rank'] for item in window] == list(range(len(window)))
        assert all(item['rerank_context'] for item in window)
        await asyncio.sleep(0.05)
        assert loads == [] and not untouched.ready
    finally:
        runtime.store.db.close()


def correction_windows(registry, token):
    """The old value scores higher than the user's later correction, in another scope."""
    key = keys(registry, registry.authenticate(token))
    return {
        key['personal']: [],
        key['sales']: [record(1, "Dana's standup meeting is at 9:30 on Mondays", 0.9, 0.03)],
        key['shared']: [record(2, "Dana's standup meeting is at 10:15 on Mondays", 0.8, 0.02)],
    }


@pytest.mark.anyio
@pytest.mark.parametrize('rerank_on', [True, False])
async def test_different_scopes_are_not_treated_as_value_corrections(registry, rerank_on):
    token = registry.issue_token('anna', 'test')
    gateway = reranker(WordModel())[0] if rerank_on else GatewayReranker(encoder_factory=lambda: None)
    service = MemoryService(registry, FusedPool(correction_windows(registry, token)), reranker=gateway)
    result = await service.call(token, 'memory_recall', {'query': 'standup meeting mondays', 'limit': 5})
    assert [item['record']['content'] for item in result['results']] == [
        "Dana's standup meeting is at 9:30 on Mondays", "Dana's standup meeting is at 10:15 on Mondays"]


@pytest.mark.anyio
@pytest.mark.parametrize('rerank_on', [True, False])
@pytest.mark.parametrize('limit', [1, 2])
@pytest.mark.parametrize('query,first', [
    ('What time was the standup meeting before?', 1),
    ('What time is the standup meeting now?', 2),
    ('Во сколько раньше был standup meeting?', 1),
])
async def test_temporal_value_order_precedes_limit(registry, rerank_on, limit, query, first):
    token = registry.issue_token('anna', 'test')
    key = keys(registry, registry.authenticate(token))
    windows = {
        key['personal']: [], key['shared']: [],
        key['sales']: [record(2, "Dana's standup meeting is at 10:15 on Mondays", 0.9, 0.03),
                       record(1, "Dana's standup meeting is at 9:30 on Mondays", 0.8, 0.02)],
    }
    gateway = reranker(WordModel())[0] if rerank_on else GatewayReranker(encoder_factory=lambda: None)
    service = MemoryService(registry, FusedPool(windows), reranker=gateway)
    result = await service.call(token, 'memory_recall', {'query': query, 'limit': limit})
    assert len(result['results']) == limit
    assert result['results'][0]['record']['id'] == first


@pytest.mark.anyio
@pytest.mark.parametrize('rerank_on', [True, False])
async def test_different_projects_do_not_reorder_each_other(registry, rerank_on):
    token = registry.issue_token('anna', 'test')
    key = keys(registry, registry.authenticate(token))
    windows = {
        key['personal']: [], key['shared']: [],
        key['sales']: [dict(record(1, "Dana's standup meeting is at 9:30 on Mondays", 0.9, 0.03), project='alpha'),
                       dict(record(2, "Dana's standup meeting is at 10:15 on Mondays", 0.8, 0.02), project='beta')],
    }
    gateway = reranker(WordModel())[0] if rerank_on else GatewayReranker(encoder_factory=lambda: None)
    service = MemoryService(registry, FusedPool(windows), reranker=gateway)
    result = await service.call(token, 'memory_recall', {'query': 'standup meeting mondays', 'limit': 2})
    assert [item['record']['id'] for item in result['results']] == [1, 2]
