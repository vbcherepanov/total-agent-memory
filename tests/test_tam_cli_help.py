"""`tam --help` and `tam --version` answer without starting the server or creating a store."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRY = 'import sys; sys.argv = ["tam", *sys.argv[1:]]; from total_agent_memory.server import main_sync; main_sync()'


def run_tam(tmp_path: Path, argv: list[str], stdin_text: str = "") -> tuple[subprocess.CompletedProcess, Path]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key not in ("CI", "GITHUB_ACTIONS", "MCP_TRANSPORT")}
    env.update(HOME=str(home), USERPROFILE=str(home), TAM_MEMORY_DIR=str(home / ".tam"),
               MEMORY_ASYNC_ENRICHMENT="false", PYTHONUTF8="1")
    completed = subprocess.run([sys.executable, "-c", ENTRY, *argv], input=stdin_text, capture_output=True,
                               text=True, env=env, cwd=str(ROOT), timeout=120, check=False)
    return completed, home


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


@pytest.mark.parametrize("flag", ["--help", "-h", "help"])
def test_help_prints_usage_and_leaves_no_store(tmp_path, flag):
    completed, home = run_tam(tmp_path, [flag])
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("usage: tam")
    assert "tam setup --help" in completed.stdout
    assert not (home / ".tam").exists()


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_prints_the_project_version_and_leaves_no_store(tmp_path, flag):
    completed, home = run_tam(tmp_path, [flag])
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == f"total-agent-memory {project_version()}"
    assert not (home / ".tam").exists()


def test_every_subcommand_in_the_help_is_dispatched(tmp_path):
    from total_agent_memory import server

    listed = {line.split()[0] for line in server.USAGE.split("commands:")[1].split("options:")[0].splitlines()
              if line.strip()}
    assert listed == set(server.SUBCOMMANDS)


def test_an_unknown_argument_is_reported_and_the_server_still_starts(tmp_path):
    frames = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}]
    completed, _ = run_tam(tmp_path, ["doctor"], "".join(json.dumps(frame) + "\n" for frame in frames))
    assert "ignoring unknown argument(s): doctor" in completed.stderr
    replies = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    assert any(reply.get("id") == 1 and "result" in reply for reply in replies), completed.stderr[-2000:]
