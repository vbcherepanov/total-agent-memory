"""Shared warm daemon (`tam daemon`) and its stdio front end (`tam proxy`).

Integration tests run real processes against a temporary memory dir on
random loopback ports and stop every daemon they start.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ENTRY = SRC / "tam_daemon"
sys.path.insert(0, str(SRC))

from tam_daemon import hook_recall, proxy, registry
from tam_daemon.service import StoreGate
from tam_daemon.settings import DaemonConfigError, DaemonSettings
from version import VERSION

httpx = pytest.importorskip("httpx")
CLI_TIMEOUT_S = 120
WAIT_TIMEOUT_S = 60
POLL_S = 0.2
FAKE_VERSION = "0.0.0-daemon-test"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def occupy(port: int) -> socket.socket:
    """Bind `port` without listening: the daemon cannot bind it and connections are refused."""
    deadline = time.monotonic() + WAIT_TIMEOUT_S
    while True:
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
            return sock
        except OSError:
            sock.close()
            if time.monotonic() > deadline:
                raise
            time.sleep(POLL_S)


class Harness:
    """One memory dir, its env and CLI; kills whatever daemon it left behind."""

    def __init__(self, memory_dir: Path, **extra_env: str):
        self.memory_dir = memory_dir
        self.paths = registry.DaemonPaths.for_memory_dir(memory_dir)
        self.env = dict(os.environ, TAM_MEMORY_DIR=str(memory_dir), MEMORY_MODE="fast", MEMORY_LLM_ENABLED="false",
                        MEMORY_CROSS_RERANK="off", TAM_DAEMON_START_TIMEOUT_SEC="60")
        self.env.update(extra_env)
        self.env.pop("MCP_TRANSPORT", None)

    def cli(self, *args: str, entry: Path = ENTRY) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(entry), *args], env=self.env, capture_output=True, text=True,
                              timeout=CLI_TIMEOUT_S, check=False)

    def status(self) -> dict:
        out = self.cli("daemon", "status", "--json")
        return json.loads(out.stdout)

    def token(self) -> str:
        return self.paths.token.read_text(encoding="utf-8").strip()

    def base(self) -> str:
        return f"http://127.0.0.1:{self.status()['port']}"

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def wait_stopped(self, timeout: float = WAIT_TIMEOUT_S) -> bool:
        deadline = time.monotonic() + timeout
        while registry.lock_is_held(self.paths.lock):
            if time.monotonic() > deadline:
                return False
            time.sleep(POLL_S)
        return True

    def cleanup(self) -> None:
        if not registry.lock_is_held(self.paths.lock):
            return
        self.cli("daemon", "stop")
        if self.wait_stopped(10):
            return
        record = registry.read_record(self.paths)
        if record is not None:
            # Windows has no SIGKILL; os.kill there is TerminateProcess for any signal.
            os.kill(record.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            self.wait_stopped(10)

    def proxy_params(self, entry: Path = ENTRY):
        from mcp import StdioServerParameters
        return StdioServerParameters(command=sys.executable, args=[str(entry), "proxy"], env=self.env)


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path / "mem")
    yield h
    h.cleanup()


def mcp_session(params):
    """An initialized ClientSession over the proxy's stdio (async context manager)."""
    from contextlib import asynccontextmanager

    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    @asynccontextmanager
    async def session():
        # The proxy's JSON logs land in pytest's captured stderr, shown only for failing tests.
        async with stdio_client(params, errlog=sys.stderr) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            yield s

    return session()


async def save_and_recall(session, content: str, project: str) -> list[str]:
    saved = await session.call_tool("memory_save", {"content": content, "type": "fact", "project": project})
    assert not saved.is_error, saved.content
    assert json.loads(saved.content[0].text)["saved"] is True
    found = await session.call_tool("memory_recall", {"query": content.split()[1], "project": project})
    assert not found.is_error, found.content
    data = json.loads(found.content[0].text)
    return [hit["content"] for group in data["results"].values() for hit in group]


# ── unit: settings ────────────────────────────────────────────────────


def test_settings_defaults_and_validation():
    settings = DaemonSettings.from_env({})
    assert settings.host == "127.0.0.1" and settings.port == 0
    assert settings.idle_exit_sec == 3600
    assert DaemonSettings.from_env({"TAM_DAEMON_IDLE_EXIT_MIN": "0"}).idle_exit_sec == 0
    assert DaemonSettings.from_env({"TAM_DAEMON_IDLE_EXIT_MIN": "0.5"}).idle_exit_sec == 30
    for env in ({"TAM_DAEMON_PORT": "70000"}, {"TAM_DAEMON_PORT": "x"}, {"TAM_DAEMON_IDLE_EXIT_MIN": "-1"},
                {"TAM_DAEMON_START_TIMEOUT_SEC": "0"}, {"TAM_DAEMON_IDLE_EXIT_MIN": "nan"}):
        with pytest.raises(DaemonConfigError):
            DaemonSettings.from_env(env)


# ── unit: registry ────────────────────────────────────────────────────


def test_token_is_private_and_persisted(tmp_path):
    paths = registry.DaemonPaths.for_memory_dir(tmp_path)
    token = registry.load_or_create_token(paths)
    assert len(token) >= 32
    assert registry.load_or_create_token(paths) == token
    if os.name == "posix":
        assert paths.token.stat().st_mode & 0o777 == 0o600
        assert paths.root.stat().st_mode & 0o777 == 0o700


def test_file_lock_is_exclusive(tmp_path):
    path = tmp_path / "x.lock"
    first, second = registry.FileLock(path), registry.FileLock(path)
    assert first.try_acquire()
    assert registry.lock_is_held(path)
    assert not second.try_acquire()
    first.release()
    assert not registry.lock_is_held(path)
    assert second.try_acquire()
    second.release()


def test_malformed_registry_is_reported(tmp_path):
    paths = registry.DaemonPaths.for_memory_dir(tmp_path)
    paths.ensure_root()
    paths.registry.write_text('{"pid": "x"}', encoding="utf-8")
    with pytest.raises(registry.RegistryError):
        registry.read_record(paths)


# ── unit: hook/recall contract ────────────────────────────────────────


def test_hook_request_validation():
    req = hook_recall.parse_request(b'{"query": "  deploy steps  ", "project": "api", "session_id": "s"}')
    assert (req.query, req.project, req.session_id) == ("deploy steps", "api", "s")
    assert req.budget_chars == hook_recall.DEFAULT_BUDGET_CHARS and req.dedupe is True
    assert req.recall_arguments() == {"query": "deploy steps", "limit": hook_recall.DEFAULT_LIMIT,
                                      "detail": "full", "project": "api"}
    for bad in (b"[]", b"not json", b'{"query": ""}', b'{"query": "q", "budget_chars": 5}',
                b'{"query": "q", "limit": true}', b'{"query": "q", "extra": 1}', b'{"query": "q", "dedupe": "y"}'):
        with pytest.raises(hook_recall.HookRequestError):
            hook_recall.parse_request(bad)
    long_query = hook_recall.parse_request(json.dumps({"query": "x" * 10000}).encode())
    assert len(long_query.query) == hook_recall.MAX_QUERY_CHARS


def test_compact_block_ranks_dedupes_and_respects_budget():
    result = {"results": {
        "fact": [{"id": 1, "content": "low " * 50, "score": 0.3, "project": "p", "created_at": "2026-01-02T00:00Z"}],
        "decision": [{"id": 2, "content": "high\nscore   decision", "score": 0.9, "project": "p"}],
    }}
    req = hook_recall.parse_request(b'{"query": "q", "budget_chars": 200}')
    block = hook_recall.compact_block(result, req)
    assert block.ids == [2, 1]
    assert block.text.splitlines()[1] == "- (decision #2, p) high score decision"
    assert len(block.text) <= 200 and block.truncated
    again = hook_recall.compact_block(result, req, seen=frozenset({2}))
    assert again.ids == [1] and again.skipped_seen == 1
    assert hook_recall.compact_block({"results": {}}, req).text == ""


def test_seen_tracker_is_bounded():
    tracker = hook_recall.SeenTracker(max_sessions=2, max_ids=3)
    tracker.remember("a", [1, 2, 3, 4])
    assert tracker.seen("a") == {2, 3, 4}
    tracker.remember("b", [1])
    tracker.remember("c", [1])
    assert tracker.seen("a") == frozenset()


# ── unit: proxy framing ───────────────────────────────────────────────


def test_header_encoding_matches_sdk_rule():
    from mcp.shared.inbound import encode_header_value
    for value in ("memory_save", "with space", " lead", "кириллица", "=?base64?abc?="):
        assert proxy.encode_header_value(value) == encode_header_value(value)


def test_headers_for_both_protocol_eras():
    record = registry.DaemonRecord(pid=1, host="127.0.0.1", port=9, version="v", started_at=0, memory_dir="/m",
                                   python="py")

    async def build():
        bridge = proxy.Bridge(proxy.daemon_client.DaemonEndpoint(record, "tok"), lambda: None, proxy.StdoutWriter())
        legacy_init = bridge.headers_for({"method": "initialize", "params": {}})
        bridge.protocol_version = "2025-06-18"
        legacy_call = bridge.headers_for({"method": "tools/call", "params": {"name": "memory_save"}})
        modern = bridge.headers_for({"method": "tools/call", "params": {
            "name": "memory_save", "_meta": {proxy.PROTOCOL_VERSION_META_KEY: "2026-07-28"}}})
        await bridge.client.aclose()
        return legacy_init, legacy_call, modern

    legacy_init, legacy_call, modern = asyncio.run(build())
    assert legacy_init["Authorization"] == "Bearer tok" and "MCP-Protocol-Version" not in legacy_init
    assert legacy_call["MCP-Protocol-Version"] == "2025-06-18" and "Mcp-Method" not in legacy_call
    assert (modern["MCP-Protocol-Version"], modern["Mcp-Method"], modern["Mcp-Name"]) == \
        ("2026-07-28", "tools/call", "memory_save")


def test_sse_parser_yields_message_events_only():
    async def lines():
        for line in (": ping", "event: message", 'data: {"jsonrpc":"2.0",', 'data: "id":1,"result":{}}', "",
                     "event: other", "data: {}", "", 'data: {"jsonrpc":"2.0","method":"n"}'):
            yield line

    async def collect():
        return [m async for m in proxy.sse_messages(lines())]

    assert asyncio.run(collect()) == [{"jsonrpc": "2.0", "id": 1, "result": {}}, {"jsonrpc": "2.0", "method": "n"}]


def test_writer_stops_once_client_stdout_is_gone():
    class Closed:
        def write(self, _data):
            raise BrokenPipeError("client gone")

        def flush(self):
            raise AssertionError("flush after failed write")

    calls = []
    writer = proxy.StdoutWriter(Closed(), on_broken=lambda: calls.append(1))
    writer.message({"jsonrpc": "2.0", "id": 1, "result": {}})
    writer.message({"jsonrpc": "2.0", "id": 2, "result": {}})
    assert writer.broken and calls == [1]


def test_store_gate_lets_handshake_through():
    assert not StoreGate.needs_store(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
    assert not StoreGate.needs_store(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
    assert StoreGate.needs_store(b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{}}')
    assert StoreGate.needs_store(b'[{"jsonrpc":"2.0","method":"ping","id":4},{"jsonrpc":"2.0","method":"x","id":5}]')
    assert not StoreGate.needs_store(b"garbage")


# ── integration: lifecycle ────────────────────────────────────────────


def test_start_status_singleton_stop(harness):
    started = harness.cli("daemon", "start")
    assert started.returncode == 0, started.stderr
    assert "started" in started.stdout
    status = harness.status()
    assert status["running"] and status["version"] == VERSION
    assert status["memory_dir"] == str(harness.memory_dir.resolve())
    assert status["port"] > 0 and status["pid"] > 0 and status["uptime_sec"] >= 0
    assert status["rss_bytes"] is None or status["rss_bytes"] > 0

    again = harness.cli("daemon", "start")
    assert again.returncode == 0 and "already running" in again.stdout and str(status["pid"]) in again.stdout
    # A second daemon started directly refuses to run next to the first.
    direct = harness.cli("daemon", "serve")
    assert direct.returncode == 3

    human = harness.cli("daemon", "status")
    assert human.returncode == 0 and f"pid:        {status['pid']}" in human.stdout

    stopped = harness.cli("daemon", "stop")
    assert stopped.returncode == 0 and "stopped" in stopped.stdout
    assert harness.cli("daemon", "status").returncode == 3
    assert not harness.paths.registry.exists()
    assert "background work flushed" in harness.paths.log.read_text(encoding="utf-8")


def test_restart_replaces_the_process(harness):
    assert harness.cli("daemon", "start").returncode == 0
    first = harness.status()["pid"]
    assert harness.cli("daemon", "restart").returncode == 0
    second = harness.status()
    assert second["running"] and second["pid"] != first


def test_tam_console_script_dispatches_daemon_commands(harness):
    code = ("import sys; from total_agent_memory.server import main_sync; "
            "sys.argv = ['tam', 'daemon', 'status', '--json']; main_sync()")
    env = dict(harness.env, PYTHONPATH=str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         timeout=CLI_TIMEOUT_S, check=False)
    assert out.returncode == 3, out.stderr
    assert json.loads(out.stdout)["running"] is False
    usage = subprocess.run([sys.executable, str(ENTRY), "proxy", "--bogus"], env=harness.env, capture_output=True,
                           text=True, timeout=CLI_TIMEOUT_S, check=False)
    assert usage.returncode == 64 and "usage: tam proxy" in usage.stderr


def test_stale_registry_is_recovered(harness):
    harness.paths.ensure_root()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    registry.write_record(harness.paths, registry.DaemonRecord(
        pid=dead.pid, host="127.0.0.1", port=free_port(), version="old", started_at=time.time(),
        memory_dir=str(harness.memory_dir), python=sys.executable))
    status = harness.cli("daemon", "status", "--json")
    assert status.returncode == 3
    assert json.loads(status.stdout) == {"running": False, "memory_dir": str(harness.memory_dir.resolve()),
                                         "stale_registry_removed": True}
    assert not harness.paths.registry.exists()
    assert harness.cli("daemon", "start").returncode == 0
    assert harness.status()["version"] == VERSION


def test_idle_exit(tmp_path):
    h = Harness(tmp_path / "mem", TAM_DAEMON_IDLE_EXIT_MIN="0.03")
    try:
        assert h.cli("daemon", "start").returncode == 0
        assert h.wait_stopped(WAIT_TIMEOUT_S), "daemon did not exit when idle"
        assert '"reason": "idle"' in h.paths.log.read_text(encoding="utf-8")
        assert not h.paths.registry.exists()
    finally:
        h.cleanup()


# ── integration: HTTP surface ─────────────────────────────────────────


def test_token_and_host_are_enforced(harness):
    assert harness.cli("daemon", "start").returncode == 0
    base, auth = harness.base(), harness.auth()
    with httpx.Client(trust_env=False) as client:
        for method, path in (("GET", "/internal/health"), ("POST", "/internal/hook/recall"),
                             ("POST", "/internal/shutdown"), ("POST", "/mcp")):
            assert client.request(method, base + path).status_code == 401
            assert client.request(method, base + path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        rebound = client.get(base + "/internal/health", headers={**auth, "Host": "evil.example"})
        assert rebound.status_code == 421
        health = client.get(base + "/internal/health", headers=auth).json()
    assert health["version"] == VERSION and health["status"] in ("starting", "ready")
    assert set(health["warm"]) == {"store", "embedding", "reranker"}


def test_hook_recall_endpoint_shape(harness):
    assert harness.cli("daemon", "start").returncode == 0

    async def seed():
        async with mcp_session(harness.proxy_params()) as session:
            await save_and_recall(session, "Rotation policy: database credentials rotate every 30 days", "hookp")

    asyncio.run(seed())
    url, auth = harness.base() + "/internal/hook/recall", harness.auth()
    body = {"query": "credentials rotate", "project": "hookp", "session_id": "sess-1", "budget_chars": 500}
    with httpx.Client(trust_env=False, timeout=30) as client:
        first = client.post(url, headers=auth, json=body)
        second = client.post(url, headers=auth, json=body)
        bad = client.post(url, headers=auth, json={"query": ""})
        unknown = client.post(url, headers=auth, json={"query": "q", "nope": 1})
        automation = client.post(url, headers=auth, json={
            "query": "<task-notification><task-id>abc</task-id>credentials rotate</task-notification>"})
    assert first.status_code == 200
    data = first.json()
    assert set(data) == {"ok", "text", "count", "ids", "skipped_seen", "truncated", "elapsed_ms", "dropped_floor"}
    assert data["ok"] and data["count"] == len(data["ids"]) >= 1
    assert "database credentials rotate every 30 days" in data["text"] and len(data["text"]) <= 500
    assert second.json()["count"] == 0 and second.json()["skipped_seen"] >= 1
    assert bad.status_code == 400 and bad.json()["ok"] is False
    assert unknown.status_code == 400 and "nope" in unknown.json()["error"]
    assert automation.status_code == 200
    assert automation.json()["count"] == 0 and automation.json()["text"] == ""


# ── integration: proxy ────────────────────────────────────────────────


def test_proxy_end_to_end_starts_daemon_and_relays(harness):
    async def scenario():
        async with mcp_session(harness.proxy_params()) as session:
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"memory_save", "memory_recall"} <= names
            hits = await save_and_recall(session, "Staging deploys run from the release branch", "proxyp")
            assert "Staging deploys run from the release branch" in hits
            saved = await session.call_tool("memory_save", {"content": "Structured payload check", "type": "fact",
                                                            "project": "proxyp"})
            assert not saved.is_error, saved.content
            structured = getattr(saved, "structured_content", None)
            assert structured is None or structured == json.loads(saved.content[0].text)
            missing = await session.call_tool("no_such_tool", {})
            assert missing.is_error and "Unknown tool" in missing.content[0].text
            status = harness.status()
            assert status["running"] and status["version"] == VERSION

    asyncio.run(scenario())


# mcp's stdio_client runs the proxy in a kill-on-close Job Object without breakaway, so the tree dies with it.
@pytest.mark.skipif(sys.platform == "win32", reason="Windows Job Object of the MCP client reaps the daemon")
def test_daemon_spawned_by_the_proxy_outlives_the_session(harness):
    async def scenario():
        async with mcp_session(harness.proxy_params()) as session:
            await session.list_tools()

    asyncio.run(scenario())
    status = harness.status()
    assert status["running"] and status["version"] == VERSION


def test_proxy_reconnects_after_daemon_restart(harness):
    async def scenario():
        async with mcp_session(harness.proxy_params()) as session:
            await save_and_recall(session, "Primary region is eu-west-1", "recon")
            first_pid = harness.status()["pid"]
            assert harness.cli("daemon", "stop").returncode == 0
            hits = await save_and_recall(session, "Failover region is eu-central-1", "recon")
            assert "Failover region is eu-central-1" in hits
            assert harness.status()["pid"] != first_pid

    asyncio.run(scenario())


def test_proxy_falls_back_in_process_when_daemon_cannot_start(tmp_path):
    port = free_port()
    blocker = occupy(port)
    h = Harness(tmp_path / "mem", TAM_DAEMON_PORT=str(port), TAM_DAEMON_START_TIMEOUT_SEC="20")
    try:
        async def scenario():
            async with mcp_session(h.proxy_params()) as session:
                assert len((await session.list_tools()).tools) > 0
                hits = await save_and_recall(session, "Fallback keeps the client working", "fb")
                assert "Fallback keeps the client working" in hits

        asyncio.run(scenario())
        assert h.cli("daemon", "status").returncode == 3
        assert "cannot bind daemon socket" in h.paths.log.read_text(encoding="utf-8")
    finally:
        blocker.close()
        h.cleanup()


def test_proxy_switches_to_local_server_mid_session(tmp_path):
    h = Harness(tmp_path / "mem", TAM_DAEMON_START_TIMEOUT_SEC="3")
    # Held by the test, the start lock makes every restart attempt time out.
    start_lock = registry.FileLock(h.paths.start_lock)
    try:
        async def scenario():
            async with mcp_session(h.proxy_params()) as session:
                await save_and_recall(session, "Cache TTL is five minutes", "local")
                assert h.cli("daemon", "stop").returncode == 0
                assert start_lock.try_acquire()
                hits = await save_and_recall(session, "Queue retries three times", "local")
                assert "Queue retries three times" in hits
                assert "Cache TTL is five minutes" in await save_and_recall(session, "Cache TTL is five minutes",
                                                                            "local")

        asyncio.run(scenario())
        assert h.cli("daemon", "status").returncode == 3
    finally:
        start_lock.release()
        h.cleanup()


def fake_version_entry(tmp_path: Path, version: str) -> Path:
    """A source tree identical to this one except for version.py (symlinks + a copied tam_daemon)."""
    root = tmp_path / "tree"
    src = root / "src"
    src.mkdir(parents=True)
    for entry in ROOT.iterdir():
        if entry.name not in ("src", ".git"):
            (root / entry.name).symlink_to(entry)
    for entry in SRC.iterdir():
        if entry.name not in ("version.py", "tam_daemon", "__pycache__"):
            (src / entry.name).symlink_to(entry)
    shutil.copytree(ENTRY, src / "tam_daemon", ignore=shutil.ignore_patterns("__pycache__"))
    (src / "version.py").write_text(f'VERSION = "{version}"\nRELEASE_DATE = "2000-01-01"\n', encoding="utf-8")
    return src / "tam_daemon"


@pytest.mark.skipif(os.name != "posix", reason="builds the fake tree with symlinks")
def test_proxy_replaces_daemon_of_another_version(harness, tmp_path):
    old_entry = fake_version_entry(tmp_path, FAKE_VERSION)
    started = harness.cli("daemon", "start", entry=old_entry)
    assert started.returncode == 0, started.stderr
    old = harness.status()
    assert old["version"] == FAKE_VERSION

    async def scenario():
        async with mcp_session(harness.proxy_params()) as session:
            assert len((await session.list_tools()).tools) > 0

    asyncio.run(scenario())
    new = harness.status()
    assert new["version"] == VERSION and new["pid"] != old["pid"]
    # `daemon start` never replaces a running daemon; it points at restart instead.
    note = harness.cli("daemon", "start", entry=old_entry)
    assert "already running" in note.stdout and "restart" in note.stdout


# ── setup integration ─────────────────────────────────────────────────


def test_register_shared_daemon_points_clients_at_proxy(tmp_path):
    import io
    import platform

    from setup_wizard import clients
    from setup_wizard.register import main as register_main

    home = tmp_path / "home"
    home.mkdir()
    host = clients.Host(home, platform.system(), {"XDG_CONFIG_HOME": str(home / ".config")}, lambda _b: None)
    memory = tmp_path / "mem"
    argv = ["--client", "cursor", "--client", "codex", "--shared-daemon", "--memory-dir", str(memory), "--no-skills"]
    assert register_main(argv, environ={}, host=host, out=io.StringIO()) == 0
    for client_id in ("cursor", "codex"):
        entry = clients.current_entry(clients.BY_ID[client_id], host)
        assert list(entry["args"])[-1] == "proxy"
        assert entry["env"]["TAM_MEMORY_DIR"] == str(memory.resolve())
    expected = clients.proxy_entry({})
    assert expected.args[-1] == "proxy"
    if expected.command == sys.executable:
        assert Path(expected.args[0]) == ENTRY
    conflicting = ["--client", "cursor", "--shared-daemon", "--command", "x", "--memory-dir", str(memory)]
    assert register_main(conflicting, environ={}, host=host) == 64


def test_ambient_relevance_floor_boundary_and_project_label(monkeypatch):
    monkeypatch.setenv('TAM_HOOK_RECALL_MIN_SCORE', '0.2')
    monkeypatch.setenv('TAM_HOOK_RECALL_CROSS_PROJECT_MIN_SCORE', '0.6')
    req = hook_recall.parse_request(b'{"query":"ledger invoices", "project":"alpha"}')
    result = {'results': {'fact': [
        {'id': 1, 'project': 'alpha', 'content': 'Below floor', 'score': 0.199},
        {'id': 2, 'project': 'alpha', 'content': 'At floor', 'score': 0.2},
        {'id': 3, 'project': 'general', 'content': 'Below cross floor', 'score': 0.599},
        {'id': 4, 'project': 'general', 'content': 'At cross floor', 'score': 0.6},
        {'id': 5, 'project': 'alpha', 'content': 'Invalid score', 'score': float('nan')},
    ]}}
    block = hook_recall.compact_block(result, req)
    assert block.ids == [4, 2]
    assert block.dropped_floor == 3
    assert '(project alpha)' not in block.text
    assert '(fact #4, general)' in block.text


def test_ambient_below_floor_returns_empty(monkeypatch):
    monkeypatch.setenv('TAM_HOOK_RECALL_MIN_SCORE', '0.3')
    req = hook_recall.parse_request(b'{"query":"ledger invoices"}')
    block = hook_recall.compact_block({'results': {'fact': [{'id': 1, 'score': 0.2}]}}, req)
    assert block.text == '' and block.ids == [] and block.dropped_floor == 1


@pytest.mark.parametrize('raw', ['nan', 'inf', '-1', 'broken'])
def test_invalid_floor_uses_safe_default(monkeypatch, raw):
    monkeypatch.setenv('TAM_HOOK_RECALL_MIN_SCORE', raw)
    req = hook_recall.parse_request(b'{"query":"ledger invoices"}')
    block = hook_recall.compact_block({'results': {'fact': [{'id': 1, 'score': 0.1}]}}, req)
    assert block.ids == [] and block.dropped_floor == 1


def test_ambient_cross_project_records_name_their_own_project_and_header_claims_none(monkeypatch):
    monkeypatch.setenv('TAM_HOOK_RECALL_MIN_SCORE', '0.2')
    monkeypatch.setenv('TAM_HOOK_RECALL_CROSS_PROJECT_MIN_SCORE', '0.6')
    req = hook_recall.parse_request(b'{"query":"ledger invoices", "project":"alpha"}')
    own = 'Alpha ledger records are reconciled every night by the batch job.'
    foreign = 'Beta invoices are exported to the accounting system weekly.'
    result = {'results': {'fact': [
        {'id': 1, 'project': 'alpha', 'content': own, 'score': 0.5},
        {'id': 2, 'project': 'beta', 'content': foreign, 'score': 0.9},
    ]}}
    block = hook_recall.compact_block(result, req)
    lines = {line.split(') ', 1)[1]: line for line in block.text.splitlines()[1:]}
    assert '(fact #2, beta)' in lines[foreign]
    assert '(fact #1, alpha)' in lines[own]
    assert 'project alpha' not in block.text.splitlines()[0]
