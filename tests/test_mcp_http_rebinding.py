"""MCP Streamable HTTP transport: DNS-rebinding protection and a minimal /healthz.

The transport has no authentication, so a browser page on a rebound domain
(its own name in Host) or a foreign Origin must be refused before any tool runs.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src" / "server.py"
sys.path.insert(0, str(ROOT / "src"))
from version import VERSION

READY_TIMEOUT_S = 120
STOP_TIMEOUT_S = 30
httpx = pytest.importorskip("httpx")
streamable_http = pytest.importorskip("mcp.client.streamable_http")
INITIALIZE = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}}})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(tmp_path: Path, bind: str, extra: dict[str, str]) -> tuple[subprocess.Popen, int]:
    port = free_port()
    env = {k: v for k, v in os.environ.items() if k != "MCP_HTTP_ALLOWED_HOSTS"}
    env.update(TAM_MEMORY_DIR=str(tmp_path), MEMORY_MODE="fast", MEMORY_LLM_ENABLED="false", MCP_TRANSPORT="http",
               MCP_HTTP_HOST=bind, MCP_HTTP_PORT=str(port), MCP_HTTP_WORKERS="1", **extra)
    proc = subprocess.Popen([sys.executable, str(SERVER)], env=env, cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + READY_TIMEOUT_S
    while True:
        assert proc.poll() is None, "server exited during startup"
        assert time.time() < deadline, "server not ready"
        try:
            httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).raise_for_status()
            return proc, port
        except httpx.HTTPError:
            time.sleep(0.5)


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=STOP_TIMEOUT_S)


@pytest.fixture(scope="module")
def loopback(tmp_path_factory: pytest.TempPathFactory):
    proc, port = start(tmp_path_factory.mktemp("mcp_loopback"), "127.0.0.1", {})
    yield port
    stop(proc)


# A non-loopback bind requires a bearer token (src/http_auth.py).
WILDCARD_TOKEN = "wildcard-test-token-0123456789-abcdef"


@pytest.fixture(scope="module")
def wildcard(tmp_path_factory: pytest.TempPathFactory):
    proc, port = start(tmp_path_factory.mktemp("mcp_wildcard"), "0.0.0.0", {"MCP_HTTP_ALLOWED_HOSTS": "mem.test", "TAM_HTTP_TOKEN": WILDCARD_TOKEN})
    yield port
    stop(proc)


def post_initialize(port: int, host: str | None, origin: str | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest("POST", "/mcp/", skip_host=True, skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", host)
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Content-Length": str(len(INITIALIZE)), "Authorization": f"Bearer {WILDCARD_TOKEN}"}
        if origin is not None:
            headers["Origin"] = origin
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.endheaders(INITIALIZE.encode())
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


async def list_tools(url: str, token: str | None = None) -> list[str]:
    from mcp import ClientSession

    client = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, follow_redirects=True) if token else None
    async with streamable_http.streamable_http_client(url, http_client=client) as (read, write), \
            ClientSession(read, write) as session:
        await session.initialize()
        return [tool.name for tool in (await session.list_tools()).tools]


def test_healthz_reports_status_version_and_capabilities(loopback: int) -> None:
    from server_capabilities import PERSONAL_CAPABILITIES
    assert httpx.get(f"http://127.0.0.1:{loopback}/healthz", timeout=5).json() == {
        "status": "ok", "version": VERSION, "capabilities": list(PERSONAL_CAPABILITIES)}


@pytest.mark.parametrize("host", ("127.0.0.1:{port}", "localhost:{port}", "127.0.0.1"))
def test_loopback_host_initializes(loopback: int, host: str) -> None:
    status, body = post_initialize(loopback, host.format(port=loopback))
    assert status == 200 and b"total-agent-memory" in body


@pytest.mark.parametrize("host", ("evil.example:{port}", "127.0.0.1.evil.example:{port}", "10.0.0.7:{port}",
                                  "mem.test:{port}", None))
def test_foreign_host_is_refused(loopback: int, host: str | None) -> None:
    status, body = post_initialize(loopback, None if host is None else host.format(port=loopback))
    assert status in (400, 421) if host is None else status == 421
    assert b"total-agent-memory" not in body


@pytest.mark.parametrize("origin", ("http://evil.example", "null", "http://127.0.0.1.evil.example:{port}",
                                    "file://"))
def test_foreign_origin_is_refused(loopback: int, origin: str) -> None:
    status, body = post_initialize(loopback, f"127.0.0.1:{loopback}", origin.format(port=loopback))
    assert status == 403 and b"total-agent-memory" not in body


def test_same_origin_is_allowed(loopback: int) -> None:
    assert post_initialize(loopback, f"localhost:{loopback}", f"http://localhost:{loopback}")[0] == 200


def test_real_mcp_client_initializes(loopback: int) -> None:
    tools = asyncio.run(list_tools(f"http://127.0.0.1:{loopback}/mcp"))
    assert "memory_recall" in tools and "memory_save" in tools


def test_wildcard_bind_accepts_listed_host_only(wildcard: int) -> None:
    assert post_initialize(wildcard, f"mem.test:{wildcard}")[0] == 200
    assert post_initialize(wildcard, f"mem.test:{wildcard}", f"http://mem.test:{wildcard}")[0] == 200
    assert post_initialize(wildcard, f"127.0.0.1:{wildcard}")[0] == 200
    assert post_initialize(wildcard, f"0.0.0.0:{wildcard}")[0] == 421
    assert post_initialize(wildcard, f"evil.example:{wildcard}")[0] == 421
    assert post_initialize(wildcard, f"mem.test:{wildcard}", "http://evil.example")[0] == 403
    assert "memory_recall" in asyncio.run(list_tools(f"http://127.0.0.1:{wildcard}/mcp", WILDCARD_TOKEN))


# In-process checks of the helpers and of the fallback guard for mcp releases without TransportSecuritySettings.

@pytest.fixture(scope="module")
def server_module(tmp_path_factory: pytest.TempPathFactory):
    os.environ.setdefault("TAM_MEMORY_DIR", str(tmp_path_factory.mktemp("mcp_mod")))
    import server
    return server


def test_allowed_hosts_cover_loopback_bind_and_env(server_module) -> None:
    hosts, origins = server_module._http_allowed_hosts("0.0.0.0", {})
    assert hosts == ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
    assert "http://localhost:*" in origins and "https://127.0.0.1:*" in origins
    assert server_module._http_allowed_hosts("::", {})[0] == hosts
    assert "192.168.1.5:*" in server_module._http_allowed_hosts("192.168.1.5", {})[0]
    assert "[fd00::5]:*" in server_module._http_allowed_hosts("fd00::5", {})[0]
    extra, _ = server_module._http_allowed_hosts("127.0.0.1", {"MCP_HTTP_ALLOWED_HOSTS": " Mem.Lan , box.example.:9,,"})
    assert {"mem.lan", "mem.lan:*", "box.example", "box.example:*"} <= set(extra)


@pytest.mark.parametrize("value", ("127.0.0.1:3737", "localhost", "[::1]:1", "evil.example:3737",
                                   "127.0.0.1.evil.example:3737", "localhost:", "", None))
def test_fallback_matching_equals_mcp_middleware(server_module, value: str | None) -> None:
    security = pytest.importorskip("mcp.server.transport_security")
    hosts, origins = server_module._http_allowed_hosts("127.0.0.1", {})
    reference = security.TransportSecurityMiddleware(security.TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins))
    assert server_module._http_pattern_match(value, hosts) == reference._validate_host(value)
    if value:
        origin = "http://" + value
        assert server_module._http_pattern_match(origin, origins) == reference._validate_origin(origin)


def test_fallback_guard_refuses_foreign_host_and_origin(server_module) -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount
    from starlette.testclient import TestClient

    async def inner(scope, receive, send):
        await PlainTextResponse("tool ran")(scope, receive, send)

    hosts, origins = server_module._http_allowed_hosts("127.0.0.1", {})
    guarded = Starlette(routes=[Mount("/mcp", app=server_module._RebindingGuard(inner, hosts, origins))])
    with TestClient(guarded, base_url="http://127.0.0.1:3737") as client:
        assert client.post("/mcp/").text == "tool ran"
        assert client.post("/mcp/", headers={"Origin": "http://localhost:3737"}).status_code == 200
        assert client.post("/mcp/", headers={"Host": "evil.example:3737"}).status_code == 421
        refused = client.post("/mcp/", headers={"Origin": "http://evil.example"})
        assert refused.status_code == 403 and refused.text != "tool ran"


def test_security_settings_are_used_when_mcp_supports_them(server_module, monkeypatch) -> None:
    import mcp.server.streamable_http_manager as manager_module

    kwargs = server_module._transport_security_kwargs(["127.0.0.1"], ["http://127.0.0.1"])
    assert kwargs["security_settings"].enable_dns_rebinding_protection is True

    class OldManager:
        def __init__(self, app, event_store=None, json_response=False, stateless=False):
            self.app = app

    monkeypatch.setattr(manager_module, "StreamableHTTPSessionManager", OldManager)
    assert server_module._transport_security_kwargs(["127.0.0.1"], ["http://127.0.0.1"]) == {}
