"""Pins the documented scope of request_id idempotency (docs/team-server-reference.md).

The request table lives in each workspace's SQLite file, in the same transaction as the write.
A repeated request_id is therefore recognised within one scope only; another scope runs it anew.
"""
import uuid

import pytest

from team_memory.contracts import Conflict
from team_memory.registry import Registry
from team_memory.service import MemoryService
from team_memory.worker import WorkerPool

TEAM = {'kind': 'team', 'team_id': 'engineering'}
SHARED = {'kind': 'shared'}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_request_id_is_recognised_within_one_scope_only(tmp_path, monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_ENABLED', 'false')
    monkeypatch.setenv('MEMORY_QUALITY_GATE_ENABLED', 'false')
    registry = Registry(tmp_path)
    registry.add_user('vasya', 'Vasya')
    registry.add_team('engineering', 'Engineering')
    registry.membership('vasya', 'engineering', 'editor')
    token = registry.issue_token('vasya', 'idempotency-test')
    pool = WorkerPool(registry.root, maximum=3)
    service = MemoryService(registry, pool)
    request_id = str(uuid.uuid4())
    save = {'content': 'The release checklist lives in the wiki.', 'request_id': request_id}
    try:
        first = await service.call(token, 'memory_save', save)
        assert await service.call(token, 'memory_save', save) == first
        with pytest.raises(Conflict):
            await service.call(token, 'memory_save', {**save, 'content': 'A different payload.'})

        other_payload = await service.call(token, 'memory_save', {**save, 'scope': SHARED, 'content': 'Shared note.'})
        assert other_payload['scope']['kind'] == 'shared' and other_payload['data']['saved'] is True

        same_payload = await service.call(token, 'memory_save', {**save, 'scope': TEAM})
        assert same_payload['scope']['team_id'] == 'engineering' and same_payload['data']['saved'] is True
        assert same_payload['data']['deduplicated'] is False
        assert await service.call(token, 'memory_save', {**save, 'scope': TEAM}) == same_payload
        personal = await service.call(token, 'memory_export', {})
        assert [r['content'] for r in personal['data']] == [save['content']]
    finally:
        pool.close()
