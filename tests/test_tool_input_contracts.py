"""MCP tool contracts: empty writes refused, scoped updates, back-dated facts, explicit supersede outcome."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests.pg_store_support import store_backend  # noqa: F401 — fixture

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def live_server(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    for sub in ("raw", "blobs", "chroma"):
        (tmp_path / sub).mkdir(exist_ok=True)
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store(database=store_backend)
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "recall", server.Recall(store))
    monkeypatch.setattr(server, "SID", "sess-contracts")
    monkeypatch.setattr(server, "BRANCH", "")
    monkeypatch.setattr(server, "_v5_modules", {})
    store.db.execute("INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, ?, ?)",
                     (server.SID, "2026-09-28T00:00:00Z", "p", "open"))
    store.db.commit()
    yield server, store
    store.db.close()


def call(server, tool, **args):
    content, is_error = asyncio.run(server._call_tool_impl(tool, args))
    text = content[0].text
    try:
        return json.loads(text), is_error
    except ValueError:
        return text, is_error


def active_count(store):
    return store.db.execute("SELECT COUNT(*) FROM knowledge WHERE status='active'").fetchone()[0]


def test_skill_update_unknown_id_has_no_usage_side_effect(live_server):
    server, store = live_server
    result, error = call(server, "memory_skill_update", skill_id="no-such-skill", success=True)
    assert error
    assert "skill" in str(result).lower() and "no-such-skill" in str(result)
    assert store.db.execute("SELECT COUNT(*) FROM skill_uses").fetchone()[0] == 0


def test_skill_store_rejects_unknown_id(live_server):
    server, store = live_server
    skills = server._get_v5("skills", store.db)
    with pytest.raises(ValueError, match="no-such-skill"):
        skills.record_use("no-such-skill", success=True)
    assert store.db.execute("SELECT COUNT(*) FROM skill_uses").fetchone()[0] == 0


def test_update_schema_requires_id_or_find():
    import jsonschema
    import server
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "memory_update")
    schema = getattr(tool, "input_schema", None) or tool.inputSchema
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"new_content": "x"}, schema)
    for selector in ({"id": 1}, {"find": "old text"}, {"id": 1, "find": "old text"}):
        jsonschema.validate({"new_content": "x", **selector}, schema)


def test_update_missing_selector_names_both_options(live_server):
    server, store = live_server
    result, error = call(server, "memory_update", new_content="replacement")
    assert error
    assert "id" in str(result) and "find" in str(result)
    assert active_count(store) == 0


def test_episode_query_uses_all_terms_and_literal_wildcards(live_server):
    server, store = live_server
    episodes = server._get_v5("episodes", store.db)
    eid = episodes.save(session_id=server.SID, project="demo", outcome="breakthrough",
                        narrative="Deploy failed on stale proto; regenerated pb.go and reached 100%.")
    episodes.save(session_id=server.SID, project="other", outcome="breakthrough",
                  narrative="Deploy failed on stale proto; regenerated pb.go and reached 100%.")
    for query in ("PROTO regenerate", "regenerate proto", "100%"):
        result, error = call(server, "memory_episode_recall", query=query, project="demo")
        assert not error
        assert [e["id"] for e in result["episodes"]] == [eid]
    for query in ("proto absent", "100_", "10%", "proto regenerate!"):
        result, error = call(server, "memory_episode_recall", query=query, project="demo")
        assert not error
        assert result["episodes"] == []


def test_associate_does_not_seed_substring_concepts(live_server):
    server, store = live_server
    graph = server._get_v5("graph_store", store.db)
    for name in ("cli", "house_style", "clickhouse"):
        graph.add_node("concept", name)
    result, error = call(server, "memory_associate", query="ClickHouse insert overload", project="demo")
    assert not error
    assert result["query_concepts"] == ["clickhouse"]


@pytest.mark.parametrize("tool", ["memory_save", "memory_save_fast"])
@pytest.mark.parametrize("content", ["", "   ", "\n\t"])
def test_blank_content_is_refused(live_server, tool, content):
    server, store = live_server
    result, is_error = call(server, tool, type="fact", project="p", content=content)
    assert is_error, result
    assert "content" in str(result)
    assert active_count(store) == 0


def test_blank_update_is_refused_and_keeps_the_record(live_server):
    server, store = live_server
    saved, _ = call(server, "memory_save", type="fact", project="p", content="Deploys run on Fridays at 10:00")
    result, is_error = call(server, "memory_update", id=saved["id"], new_content="  ")
    assert is_error, result
    row = store.db.execute("SELECT status FROM knowledge WHERE id=?", (saved["id"],)).fetchone()
    assert row[0] == "active"


def test_update_by_id_replaces_exactly_that_record(live_server):
    server, store = live_server
    a, _ = call(server, "memory_save", type="fact", project="alpha", content="Staging database host is db-stage-1")
    b, _ = call(server, "memory_save", type="fact", project="beta", content="Staging database host is db-stage-9")
    result, is_error = call(server, "memory_update", id=b["id"], new_content="Staging database host is db-stage-10")
    assert not is_error, result
    assert result["old_id"] == b["id"]
    status = dict(store.db.execute("SELECT id, status FROM knowledge WHERE id IN (?, ?)", (a["id"], b["id"])).fetchall())
    assert status == {a["id"]: "active", b["id"]: "superseded"}
    new = store.db.execute("SELECT project, content FROM knowledge WHERE id=?", (result["new_id"],)).fetchone()
    assert tuple(new) == ("beta", "Staging database host is db-stage-10")


def test_update_by_find_stays_inside_the_given_project(live_server):
    server, store = live_server
    a, _ = call(server, "memory_save", type="fact", project="alpha", content="Cache TTL for sessions is 30 minutes")
    b, _ = call(server, "memory_save", type="fact", project="beta", content="Cache TTL for sessions is 15 minutes")
    result, is_error = call(server, "memory_update", find="cache TTL sessions", project="alpha",
                            new_content="Cache TTL for sessions is 45 minutes")
    assert not is_error, result
    assert result["old_id"] == a["id"]
    assert store.db.execute("SELECT status FROM knowledge WHERE id=?", (b["id"],)).fetchone()[0] == "active"


def test_update_refuses_an_id_that_is_not_active(live_server):
    server, _ = live_server
    saved, _ = call(server, "memory_save", type="fact", project="p", content="Queue is RabbitMQ 4")
    call(server, "memory_delete", id=saved["id"])
    result, is_error = call(server, "memory_update", id=saved["id"], new_content="Queue is Kafka")
    assert not is_error
    assert result["error"] == "Record not found in DB"


def test_update_needs_find_or_id(live_server):
    server, _ = live_server
    result, is_error = call(server, "memory_update", new_content="anything")
    assert is_error, result


def test_kg_add_fact_accepts_valid_from_for_point_in_time_queries(live_server):
    server, _ = live_server
    call(server, "kg_add_fact", subject="shop", predicate="uses_cache", object="Redis",
         project="p", valid_from="2025-01-01T00:00:00Z")
    call(server, "kg_add_fact", subject="shop", predicate="uses_cache", object="Memcached",
         project="p", valid_from="2026-06-01T00:00:00Z")
    past, _ = call(server, "kg_at", subject="shop", project="p", timestamp="2025-12-01T00:00:00Z")
    now, _ = call(server, "kg_at", subject="shop", project="p")
    assert [f["object"] for f in past["assertions"]] == ["Redis"]
    assert [f["object"] for f in now["assertions"]] == ["Memcached"]


def test_kg_invalidate_fact_accepts_the_end_time(live_server):
    server, _ = live_server
    call(server, "kg_add_fact", subject="shop", predicate="runs_on", object="Heroku",
         project="p", valid_from="2024-01-01T00:00:00Z")
    closed, _ = call(server, "kg_invalidate_fact", subject="shop", predicate="runs_on", object="Heroku",
                     project="p", at="2025-03-01T00:00:00Z")
    assert closed == {"closed": 1}
    before, _ = call(server, "kg_at", subject="shop", project="p", timestamp="2025-02-01T00:00:00Z")
    after, _ = call(server, "kg_at", subject="shop", project="p", timestamp="2025-04-01T00:00:00Z")
    assert [f["object"] for f in before["assertions"]] == ["Heroku"]
    assert after["assertions"] == []


def test_supersede_request_that_retires_nothing_says_so(live_server):
    server, _ = live_server
    call(server, "memory_save", type="decision", project="p",
         content="We chose Redis Streams for the order queue instead of RabbitMQ", context="WHY: ops cost")
    result, is_error = call(server, "memory_save", type="decision", project="p", supersede=True,
                            content="Billing retries use exponential backoff with jitter", context="WHY: thundering herd")
    assert not is_error
    assert "superseded" not in result
    assert "memory_update" in result["supersede_note"]


@pytest.mark.parametrize("tool", ["memory_save", "memory_save_fast"])
def test_repeat_save_preserves_tags_and_reports_replaced_id(live_server, tool):
    server, store = live_server
    content = 'The billing service listens on port 7070 on host ram3'
    first, error = call(server, tool, type='fact', project='recheck',
                        tags=['billing', 'ports'], content=content)
    assert not error
    second, error = call(server, tool, type='fact', project='recheck',
                         tags=['dup', 'ports'], content=content + '.')
    assert not error and second['deduplicated']
    tags = json.loads(store.q1('SELECT tags FROM knowledge WHERE id=?', (second['id'],))['tags'])
    assert set(tags) == {'billing', 'ports', 'dup'} and len(tags) == 3
    assert second['superseded'] == [first['id']]


def test_update_of_superseded_id_names_current_version(live_server):
    server, store = live_server
    ids = []
    for content in ('The billing service uses port 7070', 'The billing service uses port 7070.',
                    'the billing service uses port 7070'):
        saved, error = call(server, 'memory_save', type='fact', project='recheck', content=content)
        assert not error
        ids.append(saved['id'])
    result, error = call(server, 'memory_update', id=ids[0], new_content='The billing service uses port 7071')
    assert 'superseded' in result['error'].lower()
    assert result['superseded_by'] == ids[1]
    assert result['current_id'] == ids[2]
    assert result['current_status'] == 'active'
    assert active_count(store) == 1


@pytest.mark.parametrize('tool', ['memory_save', 'memory_save_fast'])
@pytest.mark.parametrize('location', ['first', 'middle', 'last'])
def test_save_warns_about_one_character_variant(live_server, tool, location):
    server, store = live_server
    original = 'Billing ledger location is rack seven'
    first, error = call(server, tool, type='fact', project='near', content=original)
    assert not error
    position = {'first': 0, 'middle': 15, 'last': len(original) - 1}[location]
    variant = original[:position] + 'X' + original[position + 1:]
    second, error = call(server, tool, type='fact', project='near', content=variant)
    assert not error and second['saved']
    assert any(row['id'] == first['id'] and row['score'] > .9 for row in second['similar'])
    assert not second.get('revived', False)


def test_save_reports_revived_deletion_only_in_same_project(live_server):
    server, store = live_server
    text = 'Billing ledger location is rack seven'
    first, _ = call(server, 'memory_save', type='fact', project='near', content=text)
    store.delete_knowledge(first['id'])
    other, error = call(server, 'memory_save', type='fact', project='other', content=text)
    assert not error and not other.get('similar') and not other.get('revived', False)
    restored, error = call(server, 'memory_save', type='fact', project='near', content=text)
    assert not error and restored['revived'] is True
    assert restored['similar'][0]['id'] == first['id']


def test_async_enrichment_preserves_replaced_id(live_server, monkeypatch):
    import enrichment_worker
    monkeypatch.setattr(enrichment_worker, '_enabled', lambda: True)
    monkeypatch.setattr(enrichment_worker, 'enqueue', lambda *args, **kwargs: None)
    server, _ = live_server
    text = 'Billing ledger location is rack seven'
    first, _ = call(server, 'memory_save', type='fact', content=text)
    second, error = call(server, 'memory_save', type='fact', content=text + '.')
    assert not error and second['superseded'] == [first['id']]


def test_similarity_failure_does_not_hide_successful_save(live_server, monkeypatch):
    import sqlite3
    import memory_core.similar as similar
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('candidate query unavailable')
    monkeypatch.setattr(similar, 'find_similar', unavailable)
    server, store = live_server
    result, error = call(server, 'memory_save', type='fact', content='Ledger backend uses database cluster seven')
    assert not error and result['saved']
    assert result['similarity_error'] == 'Similarity check unavailable; record was saved'
    assert store.q1('SELECT status FROM knowledge WHERE id=?', (result['id'],))['status'] == 'active'


def test_file_context_finds_old_decision_after_irrelevant_rows(live_server):
    server, store = live_server
    path = "USA/tools/metrics/collect.py"
    old = store.db.execute(
        "INSERT INTO knowledge(session_id,type,content,project,confidence,created_at) "
        "VALUES (?,'decision',?,'USA',0.1,'2000-01-01T00:00:00Z')",
        (server.SID, "Preserve metric labels in " + path)).lastrowid
    for index in range(25):
        store.db.execute(
            "INSERT INTO knowledge(session_id,type,content,project,confidence,created_at) "
            "VALUES (?,'decision',?,'USA',1,'2026-11-16T00:00:00Z')",
            (server.SID, f"Unrelated decision {index}"))
    store.db.commit()
    result, error = call(server, 'file_context', path=path, project='USA', limit=1)
    assert not error and result['risk_score'] > 0
    assert [row['knowledge_id'] for row in result['warnings']] == [old]


def test_analogize_finds_russian_solution_without_latin_tags(live_server):
    server, store = live_server
    text = 'Повторная доставка сообщений требует проверки идемпотентности'
    saved = store.db.execute(
        "INSERT INTO knowledge(session_id,type,content,project,created_at) "
        "VALUES (?,'solution',?,'other','2026-11-16T00:00:00Z')", (server.SID, text)).lastrowid
    store.db.commit()
    result, error = call(server, 'analogize', text=text.upper(), exclude_project='USA')
    assert not error
    assert [row['id'] for row in result['analogies']] == [saved]
