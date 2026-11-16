"""Regression tests for the v15 review findings in the personal server."""

import asyncio
import json
import sqlite3
import threading

import pytest

from memory_core.incidents import IncidentService
from memory_core.store_owner import OwnerSettings, StoreOwner
from tests.pg_store_support import store_backend  # noqa: F401

INCIDENT_CONTENT = "Scheduler outage in the routing service.\n- [ ] Verify workers"
RELEASE_TIMEOUT_SECONDS = 10
CALL_TIMEOUT_SECONDS = 5


@pytest.fixture
def live_server(store_backend, tmp_path, monkeypatch):  # noqa: F811
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    instance = server.Store(database=store_backend, background_queues=False)
    instance._embed_mode = "none"
    monkeypatch.setattr(server, "store", instance)
    monkeypatch.setattr(server, "recall", server.Recall(instance))
    monkeypatch.setattr(server, "SID", "review-v15")
    monkeypatch.setattr(server, "_v5_modules", {})
    yield server
    instance.db.close()


def call(server, name, arguments):
    return json.loads(asyncio.run(server._do(name, arguments)))


def tool_description(server, name):
    tools = asyncio.run(server.list_tools())
    return next(tool.description for tool in tools if tool.name == name)


def counts(server):
    db = server.store.db
    return (db.execute("SELECT count(*) FROM knowledge").fetchone()[0],
            db.execute("SELECT count(*) FROM knowledge_incidents").fetchone()[0])


# ── ingest_codebase must not block other calls ──────────────────────────────

@pytest.fixture
def blocked_ingest(live_server, tmp_path, monkeypatch):
    """An ingest that stays in flight until the test releases it."""
    import resource_index.tool as tool

    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv(live_server.ALLOWED_ROOTS_ENV, str(root))
    started, release = threading.Event(), threading.Event()
    state = {"released_by_test": False}

    def slow(*_args):
        started.set()
        state["released_by_test"] = release.wait(RELEASE_TIMEOUT_SECONDS)
        return {"total_chunks": 0}

    monkeypatch.setattr(tool, "execute", slow)
    monkeypatch.setattr(tool, "execute_isolated", slow)
    return live_server, root, started, release, state


async def _stats_during_ingest(server, root, started, release, state, dispatch):
    ingest = asyncio.create_task(dispatch("ingest_codebase", {"path": str(root)}))
    assert await asyncio.wait_for(asyncio.to_thread(started.wait, CALL_TIMEOUT_SECONDS), CALL_TIMEOUT_SECONDS)
    stats = await asyncio.wait_for(dispatch("memory_stats", {}), CALL_TIMEOUT_SECONDS)
    assert not ingest.done()
    release.set()
    await asyncio.wait_for(ingest, CALL_TIMEOUT_SECONDS)
    assert state["released_by_test"]
    return json.loads(stats)


def test_ingest_does_not_block_the_event_loop(blocked_ingest):
    server, root, started, release, state = blocked_ingest
    stats = asyncio.run(_stats_during_ingest(server, root, started, release, state, server._do))
    assert isinstance(stats, dict)


def test_ingest_releases_the_store_owner_for_other_calls(blocked_ingest):
    server, root, started, release, state = blocked_ingest
    if server.store.is_postgres:
        pytest.skip("the owner serves the personal SQLite Store only")

    async def noop():
        return None

    async def scenario():
        owner = StoreOwner(noop, noop, OwnerSettings())
        server._STORE_OWNER = owner
        try:
            await owner.ready()

            def on_owner(name, arguments):
                return owner.call(lambda: server._do(name, arguments))

            return await _stats_during_ingest(server, root, started, release, state, on_owner)
        finally:
            server._STORE_OWNER = None
            await owner.close()

    assert isinstance(asyncio.run(scenario()), dict)


# ── benchmark ───────────────────────────────────────────────────────────────

def test_benchmark_description_names_the_packaged_fixture(live_server):
    description = tool_description(live_server, "benchmark")
    assert "evals/scenarios" not in description
    assert "5-scenario" in description and "isolated" in description


@pytest.mark.parametrize("error", [ValueError("bad fixture"), RuntimeError("worker failed")])
def test_benchmark_default_run_reports_errors_like_locomo(live_server, monkeypatch, error):
    import evaluation.runner as runner

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(runner, "run_isolated", fail)
    assert call(live_server, "benchmark", {}) == {"status": "error", "reason": str(error)}


def test_episode_recall_description_states_matching_rules(live_server):
    description = tool_description(live_server, "memory_episode_recall")
    assert "case-insensitive" in description and "substring" in description and "no stemming" in description


# ── advisory similarity check ───────────────────────────────────────────────

def test_similarity_failure_never_fails_the_save(live_server, monkeypatch):
    import memory_core.similar as similar

    def broken(db, *_args):
        db.execute("SELECT * FROM table_that_does_not_exist").fetchall()

    monkeypatch.setattr(similar, "find_similar", broken)
    result = call(live_server, "memory_save", {
        "type": "fact", "project": "review", "content": "Billing ledger location is rack seven"})
    assert result["saved"] is True
    assert result["similarity_error"] == "Similarity check unavailable; record was saved"
    row = live_server.store.db.execute("SELECT content FROM knowledge WHERE id=?", (result["id"],)).fetchone()
    assert row["content"] == "Billing ledger location is rack seven"
    logged = live_server.store.db.execute(
        "SELECT count(*) FROM error_log WHERE component='save.similarity'").fetchone()[0]
    assert logged == 1


# ── incident rows are atomic with the record ────────────────────────────────

def _fail_after_write(monkeypatch):
    original = IncidentService.write

    def write(self, identity, spec, content=None):
        original(self, identity, spec, content)
        raise sqlite3.OperationalError("injected incident write failure")

    monkeypatch.setattr(IncidentService, "write", write)


@pytest.mark.parametrize("tool", ["memory_save", "memory_save_fast"])
def test_failed_incident_write_leaves_no_record(live_server, monkeypatch, tool):
    _fail_after_write(monkeypatch)
    before = counts(live_server)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        call(live_server, tool, {"type": "incident", "project": "incidents", "content": INCIDENT_CONTENT})
    assert counts(live_server) == before
    pending = live_server.store.db.execute(
        "SELECT count(*) FROM write_intents WHERE status='pending'").fetchone()[0]
    assert pending == 0


def test_failed_incident_write_keeps_the_previous_version_active(live_server, monkeypatch):
    saved = call(live_server, "memory_save", {"type": "incident", "project": "incidents", "content": INCIDENT_CONTENT})
    before = counts(live_server)
    _fail_after_write(monkeypatch)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        call(live_server, "memory_update", {"id": saved["id"], "new_content": "Scheduler recovered.",
                                            "incident_status": "closed", "resolution": "Restarted the worker."})
    assert counts(live_server) == before
    status = live_server.store.db.execute("SELECT status FROM knowledge WHERE id=?", (saved["id"],)).fetchone()[0]
    assert status == "active"


def test_successful_incident_save_commits_both_rows(live_server):
    before = counts(live_server)
    result = call(live_server, "memory_save", {"type": "incident", "project": "incidents", "content": INCIDENT_CONTENT})
    assert counts(live_server) == (before[0] + 1, before[1] + 1)
    assert result["open_items"] == 1 and result["incident"]["incident_status"] == "open"


# ── ingest uses its own connection and its own bounded pool ─────────────────

def test_ingest_runs_on_a_dedicated_connection_that_is_closed(live_server, tmp_path, monkeypatch):
    import resource_index.tool as tool

    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv(live_server.ALLOWED_ROOTS_ENV, str(root))
    seen = []

    def capture(db, _request):
        seen.append(db)
        db.execute("SELECT 1").fetchone()
        return {"total_chunks": 0}

    monkeypatch.setattr(tool, "execute", capture)
    call(live_server, "ingest_codebase", {"path": str(root)})
    assert len(seen) == 1 and seen[0] is not live_server.store.db
    with pytest.raises(Exception):
        seen[0].execute("SELECT 1").fetchone()
    live_server.store.db.execute("SELECT 1").fetchone()


def test_ingest_closes_its_connection_when_extraction_fails(live_server, tmp_path, monkeypatch):
    import resource_index.tool as tool

    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv(live_server.ALLOWED_ROOTS_ENV, str(root))
    seen = []

    def fail(db, _request):
        seen.append(db)
        raise RuntimeError("extraction failed")

    monkeypatch.setattr(tool, "execute", fail)
    with pytest.raises(RuntimeError, match="extraction failed"):
        call(live_server, "ingest_codebase", {"path": str(root)})
    with pytest.raises(Exception):
        seen[0].execute("SELECT 1").fetchone()


def test_ingest_does_not_occupy_recall_inference_workers(blocked_ingest):
    server, root, started, release, state = blocked_ingest
    if server.store.is_postgres:
        pytest.skip("the owner serves the personal SQLite Store only")

    async def noop():
        return None

    async def scenario():
        owner = StoreOwner(noop, noop, OwnerSettings(inference_workers=1))
        server._STORE_OWNER = owner
        try:
            await owner.ready()

            async def recall_inference():
                return await owner.infer(lambda: "inferred")

            ingest = asyncio.create_task(owner.call(lambda: server._do("ingest_codebase", {"path": str(root)})))
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, CALL_TIMEOUT_SECONDS), CALL_TIMEOUT_SECONDS)
            inferred = await asyncio.wait_for(owner.call(recall_inference), CALL_TIMEOUT_SECONDS)
            assert not ingest.done()
            release.set()
            await asyncio.wait_for(ingest, CALL_TIMEOUT_SECONDS)
            return inferred
        finally:
            release.set()
            server._STORE_OWNER = None
            await owner.close()

    assert asyncio.run(scenario()) == "inferred"


def test_index_pool_size_comes_from_the_environment(live_server, monkeypatch):
    monkeypatch.setattr(live_server, "_INDEX_EXECUTOR", None)
    monkeypatch.delenv(live_server.INDEX_WORKERS_ENV, raising=False)
    assert live_server._index_executor()._max_workers == live_server.DEFAULT_INDEX_WORKERS == 1
    monkeypatch.setattr(live_server, "_INDEX_EXECUTOR", None)
    monkeypatch.setenv(live_server.INDEX_WORKERS_ENV, "2")
    assert live_server._index_executor()._max_workers == 2
    monkeypatch.setattr(live_server, "_INDEX_EXECUTOR", None)
    monkeypatch.setenv(live_server.INDEX_WORKERS_ENV, "0")
    with pytest.raises(ValueError, match=live_server.INDEX_WORKERS_ENV):
        live_server._index_executor()


# ── outbox replay keeps the incident spec ───────────────────────────────────

def _capture_intent_payload(server, monkeypatch):
    import outbox

    captured = []
    original = outbox.create_intent

    def create_intent(*args, **kwargs):
        captured.append(kwargs["payload"])
        return original(*args, **kwargs)

    monkeypatch.setattr(outbox, "create_intent", create_intent)
    return captured


def _pending_intent(server, payload):
    import outbox
    from datetime import datetime, timedelta, timezone

    intent = outbox.create_intent(server.store.db, payload=payload, session_id=payload["sid"],
                                  content=payload["content"], ktype=payload["ktype"], project=payload["project"])
    older = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
    server.store.db.execute("UPDATE write_intents SET created_at=? WHERE id=?", (older, intent.id))
    server.store.db.commit()
    return intent


def test_replayed_intent_restores_the_incident_rows(live_server, monkeypatch):
    captured = _capture_intent_payload(live_server, monkeypatch)
    saved = call(live_server, "memory_save", {
        "type": "incident", "project": "incidents", "content": INCIDENT_CONTENT,
        "detection": {"check": "tam diagnostics", "healthy_result": "ok"},
        "affected": [{"scope": "scheduler", "from": "2026-01-01", "to": "2026-01-02"}]})
    payload = json.loads(json.dumps(captured[0]))
    db = live_server.store.db
    db.execute("DELETE FROM knowledge WHERE id=?", (saved["id"],))
    db.commit()
    assert db.execute("SELECT count(*) FROM knowledge_incidents").fetchone()[0] == 0

    _pending_intent(live_server, payload)
    live_server.store._reconcile_outbox_at_startup()

    row = db.execute("SELECT id FROM knowledge WHERE type='incident' AND status='active'").fetchone()
    assert row is not None
    restored = IncidentService(db).get(row["id"])
    assert restored.detection.check == "tam diagnostics"
    assert [(w.scope, w.starts_at) for w in restored.affected] == [("scheduler", saved["incident"]["affected"][0]["from"])]
    assert db.execute("SELECT open_items FROM knowledge_incidents WHERE knowledge_id=?",
                      (row["id"],)).fetchone()[0] == 1


def test_intent_written_without_a_spec_still_replays(live_server, monkeypatch):
    captured = _capture_intent_payload(live_server, monkeypatch)
    call(live_server, "memory_save", {"type": "fact", "project": "review", "content": "Ledger sits in rack nine"})
    payload = json.loads(json.dumps(captured[0]))
    payload.pop(live_server.INTENT_INCIDENT_KEY)
    db = live_server.store.db
    db.execute("DELETE FROM knowledge WHERE project='review'")
    db.commit()

    _pending_intent(live_server, payload)
    live_server.store._reconcile_outbox_at_startup()

    row = db.execute("SELECT content FROM knowledge WHERE project='review' AND status='active'").fetchone()
    assert row["content"] == "Ledger sits in rack nine"


# ── a failed incident write on a repeat save rolls everything back ──────────

def test_failed_incident_write_on_a_confirming_repeat_rolls_back_the_confirmation(live_server, monkeypatch):
    from memory_core.incidents import IncidentSpec

    store = live_server.store
    spec = IncidentSpec()
    rid = store.save_knowledge("review-v15", INCIDENT_CONTENT, "incident", project="incidents",
                               skip_quality=True, incident_spec=spec)[0]
    store.db.execute("UPDATE knowledge SET last_confirmed='2020-01-01T00:00:00Z' WHERE id=?", (rid,))
    store.db.commit()
    before = dict(store.db.execute("SELECT * FROM knowledge WHERE id=?", (rid,)).fetchone())
    incident_before = dict(store.db.execute(
        "SELECT * FROM knowledge_incidents WHERE knowledge_id=?", (rid,)).fetchone())

    _fail_after_write(monkeypatch)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        store.save_knowledge("review-v15", INCIDENT_CONTENT, "incident", project="incidents", skip_quality=True,
                             repeat="confirm", source_format="conversation", tags=["extra-tag"],
                             incident_spec=IncidentSpec(incident_status="closed", resolution="Restarted."))

    assert dict(store.db.execute("SELECT * FROM knowledge WHERE id=?", (rid,)).fetchone()) == before
    assert dict(store.db.execute("SELECT * FROM knowledge_incidents WHERE knowledge_id=?",
                                 (rid,)).fetchone()) == incident_before


def test_failed_incident_write_on_a_replacing_repeat_keeps_the_stored_record(live_server, monkeypatch):
    from memory_core.incidents import IncidentSpec

    store = live_server.store
    rid = store.save_knowledge("review-v15", INCIDENT_CONTENT, "incident", project="incidents",
                               skip_quality=True, tags=["first"], incident_spec=IncidentSpec())[0]
    store.db.execute("UPDATE knowledge SET last_confirmed='2020-01-01T00:00:00Z' WHERE id=?", (rid,))
    store.db.commit()
    before = dict(store.db.execute("SELECT * FROM knowledge WHERE id=?", (rid,)).fetchone())
    total = store.db.execute("SELECT count(*) FROM knowledge").fetchone()[0]

    _fail_after_write(monkeypatch)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        store.save_knowledge("review-v15", INCIDENT_CONTENT, "incident", project="incidents", skip_quality=True,
                             tags=["second"], incident_spec=IncidentSpec())

    assert dict(store.db.execute("SELECT * FROM knowledge WHERE id=?", (rid,)).fetchone()) == before
    assert store.db.execute("SELECT count(*) FROM knowledge").fetchone()[0] == total
