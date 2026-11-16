"""Project isolation at the MCP recall dispatcher."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.pg_store_support import store_backend  # noqa: F401


@pytest.mark.parametrize('path', ['expansion', 'cognitive'])
@pytest.mark.parametrize('restricted', ['project', 'branch', 'tags', 'type'])
def test_mcp_expansion_obeys_search_scope(store_backend, tmp_path, monkeypatch, restricted, path):  # noqa: F811
    import server
    from graph.store import GraphStore

    monkeypatch.setattr(server, 'MEMORY_DIR', tmp_path)
    store = server.Store(database=store_backend)
    monkeypatch.setattr(server, 'store', store)
    monkeypatch.setattr(server, '_STORE_OWNER', None)
    monkeypatch.setenv('MEMORY_RECALL_EXCLUDE', '1')
    monkeypatch.setenv('MEMORY_RECALL_EXCLUDED_TAGS', 'recovery')
    monkeypatch.setattr(server, '_get_v5', lambda *args: SimpleNamespace(on_query=lambda *args, **kwargs: {}))
    try:
        graph = GraphStore(store.db)
        hub = graph.add_node('concept', 'ledger')
        ids = []
        for content in ('Alpha invoices', 'Restricted ledger', 'Alpha ledger details'):
            kid = store.db.execute(
                "INSERT INTO knowledge(session_id,type,content,project,branch,created_at) "
                "VALUES ('scope-test','fact',?,'alpha','main','2026-11-15T00:00:00Z')", (content,)
            ).lastrowid
            ids.append(kid)
            store.db.execute(
                "INSERT INTO knowledge_nodes(knowledge_id,node_id,role,strength) VALUES (?,?,'mentions',1)",
                (kid, hub))
        seed, hidden, allowed = ids
        column, value = {'project': ('project', 'beta'), 'branch': ('branch', 'private'),
                         'tags': ('tags', '["recovery"]'), 'type': ('type', 'solution')}[restricted]
        store.db.execute(f'UPDATE knowledge SET {column}=? WHERE id=?', (value, hidden))
        store.db.commit()
        if path == 'cognitive':
            solutions = [dict(store.db.execute('SELECT * FROM knowledge WHERE id=?', (kid,)).fetchone())
                         for kid in (hidden, allowed)]
            monkeypatch.setattr(server, '_get_v5', lambda *args: SimpleNamespace(
                on_query=lambda *args, **kwargs: {'available_solutions': solutions}))
        monkeypatch.setattr(server, 'recall', SimpleNamespace(search=lambda *args, **kwargs: {
            'results': {'fact': [{'id': seed, 'content': 'Alpha invoices', 'project': 'alpha'}]}, 'total': 1}))
        payload = asyncio.run(server.call_tool('memory_recall', {
            'query': 'invoices ledger', 'project': 'alpha', 'branch': 'main', 'type': 'fact',
            'expand_context': True, 'expand_budget': 1,
        }))
        result = json.loads(payload[0].text)
        records = (result.get('expansion', []) if path == 'expansion'
                   else result.get('cognitive', {}).get('additional_solutions', []))
        assert [record['id'] for record in records] == [allowed], result
    finally:
        store.db.close()
