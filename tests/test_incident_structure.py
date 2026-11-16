import asyncio
import json

import pytest

from memory_core.incidents import IncidentError, IncidentFilter, IncidentService
from tests.pg_store_support import store_backend  # noqa: F401


@pytest.fixture
def incident_server(store_backend, tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    instance = server.Store(database=store_backend, background_queues=False)
    instance._embed_mode = "none"
    monkeypatch.setattr(server, "store", instance)
    monkeypatch.setattr(server, "recall", server.Recall(instance))
    monkeypatch.setattr(server, "SID", "incident-test")
    monkeypatch.setattr(server, "_v5_modules", {})
    yield server
    instance.db.close()


def call(server, name, arguments):
    return json.loads(asyncio.run(server._do(name, arguments)))


def save(server, **fields):
    return call(server, "memory_save_fast", {"type": "incident", "project": "incidents",
        "content": "Scheduler outage in the routing service.\n- [ ] Verify workers\n- [x] Read logs\n- [ ] Check retries", **fields})


def test_incident_missing_structure_succeeds_with_hints(incident_server):
    result = save(incident_server)
    assert result["saved"] is True
    assert result["open_items"] == 2
    assert any("detection" in hint for hint in result["hints"])
    assert result["incident"]["incident_status"] == "open"


def test_incident_close_requires_resolution_before_mutating_record(incident_server):
    saved = save(incident_server)
    db = incident_server.store.db
    count = db.execute("SELECT count(*) FROM knowledge").fetchone()[0]
    with pytest.raises(IncidentError, match="resolution is required"):
        call(incident_server, "memory_update", {"id": saved["id"], "new_content": "Scheduler recovered.",
                                               "incident_status": "closed"})
    assert db.execute("SELECT count(*) FROM knowledge").fetchone()[0] == count
    assert db.execute("SELECT status FROM knowledge WHERE id=?", (saved["id"],)).fetchone()[0] == "active"
    updated = call(incident_server, "memory_update", {"id": saved["id"], "new_content": "Scheduler recovered.",
        "incident_status": "closed", "resolution": "Restarted the failed worker and verified retry completion."})
    assert updated["incident"]["incident_status"] == "closed"
    assert updated["open_items"] == 0
    assert IncidentService(db).get(saved["id"]).incident_status == "open"


def test_incident_recall_scopes_time_and_state_before_limit(incident_server):
    server = incident_server
    detection = {"check": "tam diagnostics scheduler", "healthy_result": "pending=0"}
    target = save(server, detection=detection,
        affected=[{"scope": "scheduler", "from": "2026-01-01T01:00:00+01:00", "to": "2026-01-02T00:00:00Z"}])
    save(server, project="foreign", affected=[{"scope": "scheduler", "from": "2026-01-01"}])
    save(server, content="Scheduler outage resolved in another region.", incident_status="closed", resolution="Fixed",
         affected=[{"scope": "scheduler", "from": "2026-01-01"}])
    save(server, content="Scheduler outage outside the affected time interval.",
         affected=[{"scope": "scheduler", "from": "2026-02-01"}])
    save(server, content="Scheduler outage in a distinct subsystem.",
         affected=[{"scope": "database", "from": "2026-01-01"}])
    options = {"query": "Scheduler outage", "project": "incidents", "limit": 1,
        "incident": {"status": "open", "scope": "scheduler", "from": "2026-01-01T12:00:00Z", "to": "2026-01-01T13:00:00Z"}}
    result = call(server, "memory_recall", options)
    hits = result["results"]["incident"]
    assert [row["id"] for row in hits] == [target["id"]]
    assert hits[0]["incident"]["detection"] == detection
    assert hits[0]["incident"]["affected"][0]["from"] == "2026-01-01T00:00:00.000000Z"
    assert call(server, "memory_recall", {**options, "incident": {"scope": "scheduler", "from": "2026-01-02", "to": "2026-01-03", "status": "open"}})["total"] == 0


@pytest.mark.parametrize("fields", [
    {"incident_status": "closed"}, {"incident_status": "closed", "resolution": " "},
    {"detection": {"check": "echo status"}},
    {"affected": [{"scope": "worker", "from": "bad-date"}]},
    {"affected": [{"scope": "worker", "from": "2026-02-01", "to": "2026-01-01"}]},
])
def test_invalid_incident_rejected_before_save(incident_server, fields):
    with pytest.raises(IncidentError):
        save(incident_server, **fields)
    assert incident_server.store.db.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0


def test_invalid_incident_filter_is_rejected():
    with pytest.raises(ValueError):
        IncidentFilter.model_validate({"from": "2026-02-01", "to": "2026-01-01"})


def test_incident_metadata_uses_the_same_privacy_rules_as_content(incident_server):
    result = save(incident_server, detection={"check": "diagnostics <private>hidden credential</private>",
                                            "healthy_result": "ok"}, resolution="<private>private cause</private>")
    stored = IncidentService(incident_server.store.db).get(result["id"]).model_dump_json()
    assert "hidden credential" not in stored and "private cause" not in stored
    payload = json.loads(stored)
    assert payload["detection"]["check"] == "diagnostics" and payload["resolution"] == ""
    with pytest.raises(IncidentError, match="resolution is required"):
        save(incident_server, incident_status="closed", resolution="<private>private cause</private>")


def test_incident_metadata_is_erased_with_knowledge(incident_server):
    saved = save(incident_server, affected=[{"scope": "scheduler", "from": "2026-01-01"}])
    db = incident_server.store.db
    for table in ("knowledge_incidents", "knowledge_incident_windows"):
        assert db.execute(f"SELECT count(*) FROM {table} WHERE knowledge_id=?", (saved["id"],)).fetchone()[0] == 1
    db.execute("DELETE FROM knowledge WHERE id=?", (saved["id"],))
    for table in ("knowledge_incidents", "knowledge_incident_windows"):
        assert db.execute(f"SELECT count(*) FROM {table} WHERE knowledge_id=?", (saved["id"],)).fetchone()[0] == 0
