import concurrent.futures
import json
import logging

import pytest
from starlette.testclient import TestClient

from team_memory.service import READ_ONLY_TOOLS, TOOLS
from tests.test_team_security_hardening import build


@pytest.fixture(autouse=True)
def backend(team_backend):
    return team_backend


@pytest.fixture
def gateway(tmp_path):
    registry, _accounts, _metrics, app = build(tmp_path / "gateway")
    registry.add_user("reader", "Reader")
    token = registry.issue_token("reader", "router-test")
    with TestClient(app) as client:
        yield client, token


def mcp(client, token, method, params=None, *, readonly=False):
    response = client.post("/mcp/" + ("?readonly=1" if readonly else ""),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def events(caplog):
    found = []
    for record in caplog.records:
        if record.name != "team_memory.service":
            continue
        payload = json.loads(record.getMessage())
        if payload.get("event") == "memory_call":
            found.append(payload)
    return found


def test_mcp_and_rest_share_router_result_and_one_audit_event(gateway, caplog):
    client, token = gateway
    caplog.set_level(logging.INFO, logger="team_memory.service")
    payload = {"name": "memory_scopes", "arguments": {}}
    rest = client.post("/api/call", headers={"Authorization": "Bearer " + token}, json=payload)
    assert rest.status_code == 200
    first = events(caplog)
    assert len(first) == 1
    caplog.clear()
    reply = mcp(client, token, "tools/call", payload)
    assert not reply.get("isError")
    assert json.loads(reply["content"][0]["text"]) == rest.json()
    second = events(caplog)
    assert len(second) == 1
    assert set(first[0]) == set(second[0])
    assert {key: value for key, value in first[0].items() if key != "duration_seconds"} == {
        key: value for key, value in second[0].items() if key != "duration_seconds"}


def test_readonly_hides_and_refuses_all_unclassified_tools(gateway, caplog):
    client, token = gateway
    listed = mcp(client, token, "tools/list", readonly=True)
    assert {tool["name"] for tool in listed["tools"]} == READ_ONLY_TOOLS
    assert {tool["name"] for tool in mcp(client, token, "tools/list")["tools"]} == set(TOOLS)
    caplog.set_level(logging.INFO, logger="team_memory.service")
    for name in sorted(set(TOOLS) - READ_ONLY_TOOLS | {"workspace_erase", "memory_rebuild_embeddings", "unknown"}):
        caplog.clear()
        result = mcp(client, token, "tools/call", {"name": name, "arguments": {}}, readonly=True)
        assert result["isError"] is True
        assert json.loads(result["content"][0]["text"])["code"] == "readonly"
        assert len(events(caplog)) == 1 and events(caplog)[0]["status"] == "rejected"


def test_readonly_policy_does_not_leak_between_parallel_requests(gateway):
    client, token = gateway

    def listed(readonly):
        return readonly, {tool["name"] for tool in mcp(client, token, "tools/list", readonly=readonly)["tools"]}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(listed, [True, False] * 4))
    for readonly, names in results:
        assert names == (READ_ONLY_TOOLS if readonly else set(TOOLS))


def test_destructive_admin_tools_are_unavailable_on_both_transports(gateway):
    client, token = gateway
    for name in ("workspace_erase", "memory_rebuild_embeddings", "reindex"):
        payload = {"name": name, "arguments": {}}
        rest = client.post("/api/call", headers={"Authorization": "Bearer " + token}, json=payload)
        result = mcp(client, token, "tools/call", payload)
        assert rest.status_code == 400 and result["isError"] is True
        assert json.loads(result["content"][0]["text"]) == rest.json()


def test_readonly_learning_does_not_deliver_deferred_memory_writes(gateway, monkeypatch):
    from team_memory.learning.service import LearningService

    delivered = []

    async def delivery(self, caller):
        delivered.append(caller.actor.user_id)

    monkeypatch.setattr(LearningService, "_deliver_notes", delivery)
    client, token = gateway
    payload = {"name": "onboarding_overview", "arguments": {}}
    assert not mcp(client, token, "tools/call", payload, readonly=True).get("isError")
    assert delivered == []
    assert not mcp(client, token, "tools/call", payload).get("isError")
    assert delivered == ["reader"]


@pytest.mark.parametrize("query", ["readonly=true", "readonly=", "readonly=1&readonly=0"])
def test_invalid_readonly_policy_is_refused(gateway, query):
    client, token = gateway
    result = client.post("/mcp/?" + query, headers={"Authorization": "Bearer " + token}, json={})
    assert result.status_code == 400
    assert result.json()["code"] == "invalid_request"
