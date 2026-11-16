"""MCP_HTTP_WORKERS: several HTTP server processes on one socket."""

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
SERVER = ROOT / "src" / "server.py"
sys.path.insert(0, str(ROOT / "src"))
from version import VERSION

READY_TIMEOUT_S = 120
STOP_TIMEOUT_S = 30

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="workers need fork()")
httpx = pytest.importorskip("httpx")
streamable_http = pytest.importorskip("mcp.client.streamable_http")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def children_of(pid: int) -> set[int]:
    """Child pids of `pid`: pgrep where installed, /proc otherwise (slim Linux images lack pgrep)."""
    if shutil.which("pgrep"):
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False).stdout
        return {int(line) for line in out.split()}
    children = set()
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        except OSError:
            continue  # the process exited while we were listing
        if int(fields[1]) == pid:
            children.add(int(stat.parent.name))
    return children


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def workers(tmp_path):
    port = free_port()
    env = dict(os.environ, TAM_MEMORY_DIR=str(tmp_path), MEMORY_MODE="fast", MEMORY_LLM_ENABLED="false",
               MCP_TRANSPORT="http", MCP_HTTP_HOST="127.0.0.1", MCP_HTTP_PORT=str(port), MCP_HTTP_WORKERS="2")
    proc = subprocess.Popen([sys.executable, str(SERVER)], env=env, cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + READY_TIMEOUT_S
    while True:
        assert proc.poll() is None, "server exited during startup"
        assert time.time() < deadline, "server not ready"
        try:
            health = httpx.get(f"{base}/healthz", timeout=2).json()
            if health.get("status") == "ok" and health.get("version") == VERSION:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    yield proc, base
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=STOP_TIMEOUT_S)


async def save_then_recall(base: str) -> list[str]:
    from mcp import ClientSession

    async def call(tool, arguments):
        async with streamable_http.streamable_http_client(f"{base}/mcp") as (read, write), \
                ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            assert not result.is_error, result.content
            return json.loads(result.content[0].text)

    await call("memory_save", {"content": "The backup job runs at 03:00 UTC", "type": "fact", "project": "w"})
    found = await call("memory_recall", {"query": "backup job", "project": "w", "detail": "full"})
    return [hit["content"] for group in found["results"].values() for hit in group]


def test_two_workers_serve_and_share_the_store(workers):
    proc, base = workers
    assert len(children_of(proc.pid)) == 2
    # Each call opens its own connection, so save and recall may reach different workers.
    assert "The backup job runs at 03:00 UTC" in asyncio.run(save_then_recall(base))


def test_sigterm_stops_every_worker(workers):
    proc, _ = workers
    kids = children_of(proc.pid)
    assert len(kids) == 2
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=STOP_TIMEOUT_S)
    time.sleep(1)
    assert {pid for pid in kids if alive(pid)} == set()


def test_bad_worker_count_is_rejected(tmp_path):
    env = dict(os.environ, TAM_MEMORY_DIR=str(tmp_path), MCP_TRANSPORT="http", MCP_HTTP_WORKERS="zero")
    out = subprocess.run([sys.executable, str(SERVER)], env=env, cwd=str(ROOT), capture_output=True, text=True,
                         timeout=READY_TIMEOUT_S, check=False)
    assert out.returncode != 0
    assert "MCP_HTTP_WORKERS must be a positive integer" in out.stderr
