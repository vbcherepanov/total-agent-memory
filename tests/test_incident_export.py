import json

import pytest

import server
from cli_store import open_store
from memory_core.incidents import IncidentService, IncidentSpec
from tests.test_portable_export import PASSPHRASE, export
from portable_export import import_bundle


@pytest.mark.parametrize("project", [None, "incident-export"])
def test_incident_structure_survives_bundle_merge(tmp_path, monkeypatch, project):
    source, target = tmp_path / "source", tmp_path / "target"
    monkeypatch.setattr(server, "MEMORY_DIR", source)
    spec = IncidentSpec.model_validate({"detection": {"check": "tam diagnostics", "healthy_result": "ok"},
        "affected": [{"scope": "scheduler", "from": "2026-01-01", "to": "2026-01-02"}],
        "resolution": "Replaced the broken scheduler lock", "incident_status": "closed"})
    with open_store(source) as store:
        identity = store.save_knowledge("export", "Scheduler incident repaired.\n- [ ] Review follow-up", "incident",
                                        "incident-export", skip_dedup=True, skip_quality=True)[0]
        IncidentService(store.db).save(identity, spec, "Scheduler incident repaired.\n- [ ] Review follow-up")
    bundle = tmp_path / "incident.tambundle"
    export(source, bundle, project=project)
    result = import_bundle(target, bundle, PASSPHRASE)
    assert result["imported"] == 1
    with open_store(target) as store:
        row = store.db.execute("SELECT id,type FROM knowledge").fetchone()
        assert row["type"] == "incident"
        assert IncidentService(store.db).get(row["id"]) == spec
        monkeypatch.setattr(server, "store", store)
        import asyncio
        fetched = json.loads(asyncio.run(server._do("memory_get", {"ids": [row["id"]]})))
        assert fetched["results"][0]["incident"] == spec.model_dump(by_alias=True)
        assert fetched["results"][0]["open_items"] == 1
