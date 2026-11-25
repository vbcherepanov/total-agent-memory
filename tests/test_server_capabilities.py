import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from server_capabilities import (PERSONAL_CAPABILITIES, UNREACHABLE_EXIT, UNSUPPORTED_EXIT, CapabilityFailure,
                                 required_capabilities, require_capabilities)

ROOT = Path(__file__).resolve().parents[1]


def test_remote_bridge_runs_as_a_single_copied_file(tmp_path):
    """docs/team-server.md: copy src/team_memory/remote.py to the client; it needs Python and nothing else."""
    copied = tmp_path / "remote.py"
    copied.write_bytes((ROOT / "src" / "team_memory" / "remote.py").read_bytes())
    result = subprocess.run([sys.executable, "-I", str(copied), "--help"], cwd=tmp_path, capture_output=True, text=True,
                            env={key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ},
                            timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert "--token-file" in result.stdout


@pytest.fixture
def endpoint_server():
    state = {"health": {"status": "ok"}, "posts": [], "paths": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["paths"].append(self.path)
            assert "Authorization" not in self.headers
            body = state.get("raw") or json.dumps(state["health"]).encode()
            self.send_response(state.get("status", 200))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            frame = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["posts"].append(frame)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {}}).encode())

        def log_message(self, format, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/mcp", state, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("client", ["remote", "team"])
@pytest.mark.parametrize("scenario", ["old", "unreachable", "supported", "legacy"])
def test_cli_preflight_precedes_flagged_request(tmp_path, endpoint_server, client, scenario):
    url, state, httpd = endpoint_server
    if scenario == "supported":
        state["health"]["capabilities"] = list(PERSONAL_CAPABILITIES)
    if scenario == "unreachable":
        httpd.shutdown()
        httpd.server_close()
    token = tmp_path / "token"
    token.write_text("test-token", encoding="utf-8")
    arguments = {"query": "routing"}
    if scenario != "legacy":
        arguments["as_of"] = "2026-01-01T00:00:00Z"
    frame = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "memory_recall", "arguments": arguments}}
    command = [sys.executable, "-m", "team_memory.remote"] if client == "remote" else [sys.executable, "-m", "team_memory.cli", "remote"]
    result = subprocess.run([*command, "--url", url, "--token-file", str(token)], input=json.dumps(frame) + "\n",
                            text=True, capture_output=True, timeout=20,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "TAM_MEMORY_DIR": str(tmp_path / "memory")})
    if scenario in ("old", "unreachable"):
        assert result.returncode == (2 if scenario == "old" else 3)
        assert ("Upgrade the server to version 15.0.0" if scenario == "old" else "Check the connection and retry") in result.stderr
        assert not state["posts"]
        assert json.loads(result.stdout)["error"]["code"] == -32000 - result.returncode
    else:
        assert result.returncode == 0, result.stderr
        assert state["posts"] == [frame]
        assert state["paths"] == (["/healthz"] if scenario == "supported" else [])


def test_version_dependent_arguments_have_explicit_requirements():
    assert required_capabilities({"method": "tools/call", "params": {"name": "memory_save", "arguments": {
        "content": "fact", "source_format": "conversation", "branch": "main", "request_id": "id"}}}) == (
            "records.source_format", "records.branch", "writes.request_id")
    assert required_capabilities({"method": "initialize"}) == ()


@pytest.mark.parametrize("supported", [False, True])
def test_remote_readonly_url_requires_server_support(tmp_path, endpoint_server, supported):
    url, state, _server = endpoint_server
    if supported:
        state["health"]["capabilities"] = ["endpoint.readonly"]
    token = tmp_path / "token"
    token.write_text("test-token", encoding="utf-8")
    frame = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    result = subprocess.run([sys.executable, "-m", "team_memory.remote", "--url", url + "?readonly=1",
                             "--token-file", str(token)], input=json.dumps(frame) + "\n", text=True,
                            capture_output=True, timeout=20,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert result.returncode == (0 if supported else 2), result.stderr
    assert state["posts"] == ([frame] if supported else [])
    assert state["paths"] == ["/healthz"]


@pytest.mark.parametrize(("state_update", "exit_code", "message"), [
    ({"status": 404}, UNSUPPORTED_EXIT, "Upgrade the server to version"),
    ({"status": 200}, UNSUPPORTED_EXIT, "Upgrade the server to version"),
    ({"status": 500}, UNREACHABLE_EXIT, "Check the connection and retry"),
    ({"status": 401}, UNREACHABLE_EXIT, "HTTP 401.*credentials"),
    ({"status": 403}, UNREACHABLE_EXIT, "HTTP 403.*credentials"),
    ({"status": 200, "raw": b"not json"}, UNREACHABLE_EXIT, "Check the connection and retry"),
])
def test_health_outcome_decides_upgrade_or_retry(endpoint_server, state_update, exit_code, message):
    import urllib.request

    url, state, _server = endpoint_server
    state.update(state_update)
    with pytest.raises(CapabilityFailure, match=message) as failure:
        require_capabilities(urllib.request.build_opener(), url, ["recall.snapshot"], timeout=5)
    assert failure.value.exit_code == exit_code


def test_remote_cli_reports_old_server_without_healthz_route(tmp_path, endpoint_server):
    url, state, _server = endpoint_server
    state["status"] = 404
    token = tmp_path / "token"
    token.write_text("test-token", encoding="utf-8")
    frame = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "memory_recall", "arguments": {"query": "routing", "as_of": "2026-01-01T00:00:00Z"}}}
    result = subprocess.run([sys.executable, "-m", "team_memory.remote", "--url", url, "--token-file", str(token)],
                            input=json.dumps(frame) + "\n", text=True, capture_output=True, timeout=20,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert result.returncode == UNSUPPORTED_EXIT
    assert "Upgrade the server to version 15.0.0" in result.stderr
    assert not state["posts"]


def test_refused_flagged_call_ends_the_session_before_any_later_frame_is_sent(tmp_path, endpoint_server):
    url, state, _ = endpoint_server
    token = tmp_path / "token"
    token.write_text("test-token", encoding="utf-8")
    flagged = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "memory_recall", "arguments": {"query": "x", "as_of": "2026-01-01T00:00:00Z"}}}
    later = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_recall", "arguments": {"query": "y"}}}
    result = subprocess.run([sys.executable, "-m", "team_memory.remote", "--url", url, "--token-file", str(token)],
                            input=json.dumps(flagged) + "\n" + json.dumps(later) + "\n", text=True, capture_output=True,
                            timeout=20, env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "TAM_MEMORY_DIR": str(tmp_path / "memory")})
    assert result.returncode == 2
    assert not state["posts"]
    assert [json.loads(line)["id"] for line in result.stdout.splitlines()] == [1]
